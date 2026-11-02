"""Loopback-only local map server and passive AION2 observation service."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import webbrowser

from .model import MapState
from .notify import Notifier
from .localization import test_alert
from .history_store import data_directory, legacy_histories, migrate_legacy, HistoryInUse, WriterLock
from . import __version__
from .resources import load_maps, image_path

ROOT = Path(__file__).resolve().parent


class Application:
    def __init__(self, args):
        self.args = args
        self.stopping = threading.Event()
        self.token = secrets.token_urlsafe(32)
        self.directory = data_directory(ROOT, args)
        self.readonly = bool(args.no_capture and (getattr(args, "read_only", False) or not args.data_dir) and not args.replay)
        maps = load_maps(self.directory)
        seed = None  # Public point datasets are references, never pretend live observations.
        mode = "replay" if args.replay else "offline" if args.no_capture else "live"
        self.state = MapState(self.directory, maps, seed, mode, readonly=self.readonly)
        self.url = f"http://127.0.0.1:{args.port}"
        self.notifier = Notifier(self.url, self.state.language)
        self.collector = None
        self.worker = None
        self.server = None
        self.archive_process = None

    def event(self, event):
        alert = self.state.handle(event)
        if alert and not self.args.replay:
            self.notifier.send(alert, self.state.sound)

    def snapshot(self):
        result = self.state.snapshot()
        result.update(application_id="aion2-redbag-map", process_id=os.getpid(), version=__version__)
        result["control_token"] = self.token
        result["notification"] = dict(self.notifier.status)
        return result

    def start_archive_versions(self):
        if self.args.replay or self.args.no_capture:
            return
        try:
            from .runtime import worker_command
            self.archive_process = subprocess.Popen(
                worker_command("archive", ["--data-dir", str(self.directory), "--watch",
                                          "--parent-pid", str(os.getpid()), "--wait-for-lock", "10"]),
                cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError:
            logging.exception("Local history version worker could not start; normal archive saving continues")

    def start(self):
        self.notifier.start()
        if self.args.no_capture:
            self.state.capture_status({"phase": "paused", "detail": "历史点位浏览，没有启动实时接收"})
            return
        from .collector import Collector, replay_pcap
        if self.args.replay:
            self.state.capture_status({"phase": "replay", "detail": "历史采样回放，非当前游戏实时状态"})

            def replay():
                if self.stopping.wait(self.args.replay_delay):
                    return
                try:
                    replay_pcap(Path(self.args.replay), self.event, self.state.capture_status,
                                speed=self.args.replay_speed, stop_event=self.stopping)
                    self.state.capture_status({"phase": "replay_done", "detail": "历史采样回放完成，非实时游戏状态"})
                except Exception:
                    logging.exception("Replay failed")
                    self.state.capture_status({"phase": "error", "detail": "历史回放失败，请查看本机日志"})

            self.worker = threading.Thread(target=replay, daemon=True, name="sample-replay")
            self.worker.start()
        else:
            self.collector = Collector(self.event, self.state.capture_status)
            self.collector.start()
            self.start_archive_versions()

    def control(self, body):
        action = body.get("action")
        if action in ("pause", "resume"):
            if self.args.no_capture:
                raise ValueError("当前为离线检查模式，没有启动实时采集")
            if self.args.replay:
                raise ValueError("回放检查不支持切换为实时接收")
            paused = action == "pause"
            self.state.set_paused(paused)
            if self.collector:
                self.collector.set_paused(paused)
        elif action == "map":
            value = body.get("map_id")
            if type(value) is not int:
                raise ValueError("地图编号无效")
            self.state.select_map(value)
        elif action == "sound":
            if type(body.get("enabled")) is not bool:
                raise ValueError("声音选项无效")
            self.state.set_sound(body["enabled"])
        elif action == "language":
            self.state.set_language(body.get("language"))
            self.notifier.set_language(self.state.language)
        elif action == "test_alert":
            self.notifier.send(test_alert(self.state.language), self.state.sound)
        elif action == "open_data_folder":
            try:
                os.startfile(str(self.directory))
            except (OSError, AttributeError) as exc:
                raise ValueError("无法打开存档目录，请复制页面中的保存路径。") from exc
        elif action == "shutdown":
            self.stopping.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            raise ValueError("不支持的操作")
        return {"ok": True, "action": action}

    def close(self):
        self.stopping.set()
        if self.collector:
            self.collector.stop()
        self.notifier.close()
        self.state.capture_status({"phase": "stopped", "detail": "工具已停止"})
        self.state.flush()
        if self.archive_process is not None and self.archive_process.poll() is None:
            try:
                from .history_store import atomic_write
                atomic_write(self.directory / "git-archive.stop", b"stop\n")
            except OSError:
                logging.exception("Version worker will flush when its parent exits")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, application, **kwargs):
        self.application = application
        super().__init__(*args, directory=str(ROOT / "static"), **kwargs)

    def translate_path(self, path):
        route = urlsplit(path).path
        if route.startswith("/maps/"):
            custom = image_path(self.application.directory, route[len("/maps/"):])
            if custom:
                return str(custom)
        return super().translate_path(path)

    def log_message(self, *_args):
        pass

    def end_headers(self):
        if not urlsplit(self.path).path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        super().end_headers()

    def allowed_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def json_response(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.allowed_host():
            self.json_response({"error": "仅允许本机访问"}, 403)
            return
        path = urlsplit(self.path).path
        if path == "/api/state":
            self.json_response(self.application.snapshot())
        elif path == "/api/history-export":
            origin = self.headers.get("Origin")
            local = (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}")
            if (origin and origin not in local) or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.json_response({"error": "仅允许从本机地图导出"}, 403)
                return
            raw = json.dumps(self.application.state.export_history(), ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="AION2-backpack-history-{time.strftime("%Y%m%d-%H%M%S")}.json"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif path.startswith("/api/"):
            self.json_response({"error": "不存在的接口"}, 404)
        else:
            # A buildless UI and map assets only; never expose saved observation data as files.
            super().do_GET()

    def list_directory(self, _path):
        self.send_error(404)
        return None

    def do_POST(self):
        if not self.allowed_host():
            self.json_response({"error": "仅允许本机访问"}, 403)
            return
        origin = self.headers.get("Origin")
        accepted_origins = {self.application.url, f"http://localhost:{self.server.server_port}"}
        key = self.headers.get("X-Redbag-Key", "")
        if (origin and origin not in accepted_origins) or not secrets.compare_digest(key, self.application.token):
            self.json_response({"error": "请从本机地图页面操作"}, 403)
            return
        if urlsplit(self.path).path != "/api/control":
            self.json_response({"error": "不存在的接口"}, 404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("请求长度无效")
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                raise ValueError("请求格式无效")
            self.json_response(self.application.control(body))
        except (ValueError, TypeError, KeyError) as exc:
            self.json_response({"error": str(exc)}, 400)


def main():
    parser = argparse.ArgumentParser(description="AION2 本地红色背包地图")
    parser.add_argument("--port", type=int, default=18726)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--data-dir")
    parser.add_argument("--replay", help="离线 PCAP 回放，仅用于验证")
    parser.add_argument("--replay-speed", type=float, default=1)
    parser.add_argument("--replay-delay", type=float, default=2)
    parser.add_argument("--empty", action="store_true", help="验证时不加载初始历史点")
    parser.add_argument("--no-capture", action="store_true", help="只运行界面，用于离线检查")
    parser.add_argument("--read-only", action="store_true", help="历史浏览只读本地存档")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535 or args.replay_speed < 0 or args.replay_delay < 0:
        parser.error("参数超出范围")
    directory = data_directory(ROOT, args)
    directory.mkdir(parents=True, exist_ok=True)
    readonly = bool(args.no_capture and (args.read_only or not args.data_dir) and not args.replay)
    legacy = legacy_histories(ROOT)
    try:
        # Acquire ownership before loading or writing, even if the HTTP port is free.
        with nullcontext() if readonly else WriterLock(directory):
            if not args.data_dir and not args.replay:
                if readonly:
                    try:
                        with WriterLock(directory):
                            migrate_legacy(directory, legacy)
                    except HistoryInUse:
                        pass  # The receiver owns the archive; the viewer only reads it.
                else:
                    migrate_legacy(directory, legacy)
            log_name = "history-app.log" if readonly else "app.log"
            logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(directory / log_name, maxBytes=1_000_000, backupCount=2, encoding="utf-8")],
                                format="%(asctime)s %(levelname)s %(message)s")
            app = Application(args)
            handler = partial(Handler, application=app)
            try:
                with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
                    server.daemon_threads = True
                    app.server = server
                    app.start()
                    logging.info("Local map started on port %s (mode=%s)", args.port, app.state.mode)
                    print(f"AION2 Hidden Cube Radar: {app.url}", flush=True)
                    if not args.no_browser:
                        webbrowser.open(app.url)
                    server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
            finally:
                app.close()
                logging.info("Local map stopped")
    except HistoryInUse as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
