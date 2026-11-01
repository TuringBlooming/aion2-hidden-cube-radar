"""Owned worker lifecycle and authenticated, loopback-only desktop controls."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import webbrowser

from . import __version__
from .history_store import atomic_write
from .localization import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, validate_language

APPLICATION_ID = "aion2-redbag-map"
WORKERS = {"service": "app", "overlay": "overlay", "archive": "git_archive"}


class DesktopError(RuntimeError):
    """A user-readable local application error, never containing a control token."""
    def __init__(self, message, *, key=None):
        super().__init__(message)
        self.key = key


class ServiceCollision(DesktopError):
    pass


class NpcapMissing(DesktopError):
    pass


def default_data_dir():
    return Path.home() / "Documents" / "AION2-RedBag-Map" / "data"


def worker_command(kind, args=()):
    if kind not in WORKERS:
        raise ValueError("unknown application worker")
    if isinstance(args, (str, bytes)):
        raise TypeError("worker arguments must be a sequence")
    prefix = [sys.executable]
    if not getattr(sys, "frozen", False):
        prefix += ["-m", "aion2_redbag_map"]
    return prefix + ["--worker", kind, *(str(value) for value in args)]


def npcap_available():
    """Check installation files only; do not load a driver or open an interface."""
    if os.name != "nt":
        return False
    folder = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "Npcap"
    return (folder / "wpcap.dll").is_file() and (folder / "Packet.dll").is_file()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ServiceCollision("本机连接发生跳转，已停止操作。请先关闭占用连接的其他程序。")


class LocalClient:
    def __init__(self, port, data_dir):
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("invalid local port")
        self.port = port
        self.directory = Path(data_dir).expanduser().resolve()
        self.url = f"http://127.0.0.1:{port}"
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def validate(self, data):
        if (not isinstance(data, dict) or data.get("app") != "AION2 RedBag Map"
                or data.get("application_id") != APPLICATION_ID
                or type(data.get("process_id")) is not int or data["process_id"] <= 0):
            raise ServiceCollision("此连接已被其他程序或旧版地图占用。请先关闭它，再使用此版本。")
        storage = data.get("storage")
        directory = storage.get("directory") if isinstance(storage, dict) else None
        if not isinstance(directory, str) or not directory:
            raise ServiceCollision("无法确认已有地图的数据目录，已停止操作。")
        try:
            same = os.path.normcase(str(Path(directory).expanduser().resolve())) == os.path.normcase(str(self.directory))
        except (OSError, ValueError):
            same = False
        if not same:
            raise ServiceCollision("已有地图正在使用另一个数据目录。请关闭该地图后重试。")
        return data

    def _request(self, request):
        try:
            # Windows can take about two seconds to return WSAECONNREFUSED on
            # an unused loopback port. A shorter timeout hides the idle state
            # and prevents the manager from starting its own service.
            with self.opener.open(request, timeout=4) as response:
                raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ServiceCollision("本机地图返回了异常数据，已停止操作。")
            return json.loads(raw)
        except HTTPError as exc:
            raise DesktopError("本机服务拒绝了操作，请确认没有其他版本正在运行。") from exc
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, ConnectionRefusedError) or getattr(reason, "errno", None) in (61, 111, 10061):
                return None
            raise DesktopError("暂时无法连接本机地图，请稍后重试。") from exc
        except (TimeoutError, socket.timeout, OSError) as exc:
            raise DesktopError("本机地图未及时响应，请稍后重试。") from exc
        except (ValueError, UnicodeError) as exc:
            raise ServiceCollision("此连接返回的内容不是本机地图数据。") from exc

    def snapshot(self):
        data = self._request(self.url + "/api/state")
        return self.validate(data) if data is not None else None

    def control(self, action, *, expected_pid=None, language=None):
        if action not in ("pause", "resume", "shutdown", "language"):
            raise ValueError("unsupported desktop control")
        body = {"action": action}
        if action == "language":
            body["language"] = validate_language(language)
        elif language is not None:
            raise ValueError("language is only valid for the language action")
        data = self.snapshot()
        if data is None:
            raise DesktopError("本机地图已停止，请重新开始。")
        if expected_pid is not None and data["process_id"] != expected_pid:
            raise ServiceCollision("原来的地图服务已被替换；当前服务保持运行。")
        token = data.get("control_token")
        if not isinstance(token, str) or not token:
            raise DesktopError("已有地图不支持安全控制，请先关闭旧版本。")
        request = Request(self.url + "/api/control", method="POST",
                          data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json", "X-Redbag-Key": token,
                                   "Origin": self.url})
        result = self._request(request)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise DesktopError("本机地图未完成操作，请稍后重试。")
        return result


class RuntimeController:
    """Only Popen handles created here may be stopped on desktop exit."""
    def __init__(self, port=18726, data_dir=None, *, client=None, process_factory=None,
                 driver_check=None, clock=None, sleep=None):
        self.directory = Path(data_dir or default_data_dir()).expanduser().resolve()
        self.port = port
        self.client = client or LocalClient(port, self.directory)
        self.process_factory = process_factory or subprocess.Popen
        self.driver_check = driver_check or npcap_available
        self.clock, self.sleep = clock or time.monotonic, sleep or time.sleep
        self.service = None
        self.overlay = None
        self.language = self._load_language()
        self.language_save_error = False

    def _load_language(self):
        # Read preferences only; never rewrite history from the manager. A later
        # web-map change in history takes precedence over an older desktop choice.
        choices = []
        for name in ("history.json", "desktop-settings.json"):
            path = self.directory / name
            try:
                data = json.loads(path.read_text("utf-8"))
                value = data.get("language") if isinstance(data, dict) else None
                if value in SUPPORTED_LANGUAGES:
                    choices.append((path.stat().st_mtime_ns, value))
            except (OSError, ValueError):
                pass
        return max(choices, default=(0, DEFAULT_LANGUAGE))[1]

    def _remember_language(self, language):
        self.language = validate_language(language)
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            atomic_write(self.directory / "desktop-settings.json",
                         json.dumps({"language": self.language}).encode("utf-8"))
        except OSError as exc:
            self.language_save_error = True
            raise DesktopError("语言设置未能保存，请检查数据目录是否可写。", key="language_save_error") from exc
        self.language_save_error = False

    def set_language(self, language):
        language = validate_language(language)
        state = self.client.snapshot()
        if state is not None:
            self.client.control("language", expected_pid=state["process_id"], language=language)
        self._remember_language(language)

    def _spawn(self, kind, args):
        self.directory.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["PYTHONUTF8"] = "1"
        if not getattr(sys, "frozen", False):
            source = str(Path(__file__).resolve().parent.parent)
            environment["PYTHONPATH"] = source + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
        with (self.directory / "desktop-worker.log").open("ab") as log:
            return self.process_factory(worker_command(kind, args), stdin=subprocess.DEVNULL,
                                        stdout=log, stderr=log, env=environment,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    @staticmethod
    def _running(process):
        return process is not None and process.poll() is None

    def _start_service(self, *, history):
        args = ["--port", self.port, "--data-dir", self.directory, "--no-browser"]
        if history:
            args += ["--no-capture", "--read-only"]
        self.service = self._spawn("service", args)
        deadline = self.clock() + 10
        try:
            while self.clock() < deadline:
                if self.service.poll() is not None:
                    raise DesktopError("地图未能启动。请查看数据目录中的 desktop-worker.log。")
                state = self.client.snapshot()
                if state:
                    if state["process_id"] != self.service.pid:
                        raise ServiceCollision("另一份地图先启动了；本次启动已取消，已有服务保持运行。")
                    if state.get("language", DEFAULT_LANGUAGE) != self.language:
                        self.client.control("language", expected_pid=self.service.pid, language=self.language)
                        state = dict(state, language=self.language)
                    return state
                self.sleep(.1)
            raise DesktopError("地图启动超时。请查看数据目录中的 desktop-worker.log。")
        except Exception:
            self._finish_process(self.service, .3)
            self.service = None
            raise

    def ensure_history(self):
        state = self.client.snapshot()
        return state if state is not None else self._start_service(history=True)

    def start_receiving(self):
        state = self.client.snapshot()
        failed = bool(state and state.get("capture", {}).get("phase") in ("error", "stopped"))
        if state and state.get("mode") == "live" and not state.get("paused") and not failed:
            return state
        if not self.driver_check():
            raise NpcapMissing("未找到接收组件，暂时不能实时接收，仍可浏览地图和历史。")
        if state and state.get("mode") == "live" and not failed:
            self.client.control("resume")
            return self.client.snapshot()
        if state:
            if not self._running(self.service) or state["process_id"] != self.service.pid:
                raise DesktopError("已有地图由其他窗口启动。请先关闭该窗口，再开始实时接收。")
            self._stop_service()
        return self._start_service(history=False)

    def stop_receiving(self):
        state = self.client.snapshot()
        if state and state.get("mode") == "live" and not state.get("paused"):
            self.client.control("pause")
        return self.client.snapshot()

    def open_map(self):
        self.ensure_history()
        webbrowser.open(self.client.url)

    def _overlay_status(self):
        try:
            path = self.directory / "overlay-status.json"
            if time.time() - path.stat().st_mtime > 5:
                return None
            state = json.loads(path.read_text("utf-8"))
            return state if isinstance(state, dict) and state.get("running") else None
        except (OSError, ValueError):
            return None

    def show_overlay(self):
        self.ensure_history()
        if self._running(self.overlay):
            return
        status = self._overlay_status()
        if status:
            if status.get("api") != self.client.url + "/api/state":
                raise ServiceCollision("已有悬浮地图连接到另一份服务，请先关闭该悬浮地图。")
            return  # Reuse it without assuming ownership.
        self.overlay = self._spawn("overlay", ["--port", self.port, "--data-dir", self.directory])
        self.sleep(.2)
        if self.overlay.poll() is not None:
            self.overlay = None
            raise DesktopError("悬浮地图未能打开，可能已有另一份悬浮地图。请查看数据目录中的日志。")

    def open_data_directory(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        if not hasattr(os, "startfile"):
            raise DesktopError("请在文件管理器中打开数据目录。")
        os.startfile(str(self.directory))

    def import_assets(self, folder):
        from .resources import import_assets
        return import_assets(Path(folder), self.directory)

    def status(self):
        state = self.client.snapshot()
        capture = state.get("capture", {}) if state else {}
        language = state.get("language") if state else None
        if language in SUPPORTED_LANGUAGES and (language != self.language or self.language_save_error):
            try:
                self._remember_language(language)
            except DesktopError:
                pass  # A preference write failure must not hide reception status.
        return {"connected": state is not None, "version": __version__,
                "language": self.language, "language_save_error": self.language_save_error,
                "mode": state.get("mode") if state else None,
                "paused": bool(state and state.get("paused")),
                "phase": capture.get("phase"), "detail": capture.get("detail", ""),
                "record_count": state.get("storage", {}).get("record_count", 0) if state else None,
                "storage_error": state.get("storage_error") if state else None,
                "owned_service": bool(state and self._running(self.service) and state["process_id"] == self.service.pid),
                "npcap": self.driver_check()}

    def _finish_process(self, process, timeout=4):
        if not self._running(process):
            return
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # This is our retained child handle, never a discovered PID or another application.
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def _stop_service(self):
        process = self.service
        if not self._running(process):
            self.service = None
            return
        try:
            state = self.client.snapshot()
            if state and state["process_id"] == process.pid:
                self.client.control("shutdown", expected_pid=process.pid)
        except DesktopError:
            pass
        try:
            self._finish_process(process)
        finally:
            self.service = None

    def close(self):
        try:
            if self._running(self.overlay):
                status = self._overlay_status()
                if status and status.get("pid") == self.overlay.pid:
                    try:
                        atomic_write(self.directory / "overlay.stop", json.dumps({"requested_at": time.time()}).encode("utf-8"))
                    except OSError:
                        pass  # Fall back only to the retained child handle.
                self._finish_process(self.overlay)
        finally:
            self.overlay = None
            self._stop_service()


def self_check(output):
    """A local packaging check: imports only, with no GUI, workers, or collection."""
    modules = {}
    for name in ("aion2_redbag_map.app", "aion2_redbag_map.desktop", "aion2_redbag_map.desktop_text",
                 "aion2_redbag_map.overlay", "aion2_redbag_map.git_archive",
                 "aion2_redbag_map.resources", "tkinter", "PIL.Image"):
        try:
            importlib.import_module(name)
            modules[name] = {"ok": True}
        except Exception as exc:
            modules[name] = {"ok": False, "error": type(exc).__name__}
    try:
        from .resources import ROOT, load_maps
        maps = load_maps()
        required = ("index.html", "app.js", "style.css", "i18n.js", "candidates.json", "alert.wav")
        files = {name: (ROOT / "static" / name).is_file() for name in required}
        candidates = json.loads((ROOT / "static" / "candidates.json").read_text("utf-8"))
        resources = {"ok": all(files.values()), "map_count": len(maps), "files": files,
                     "candidate_count": sum(len(row.get("points", [])) for row in candidates.get("maps", {}).values()),
                     "bundled_map_images": len(list((ROOT / "static" / "maps").rglob("*.webp")))}
    except Exception as exc:
        resources = {"ok": False, "error": type(exc).__name__}
    report = {"ok": all(value["ok"] for value in modules.values()) and resources["ok"], "version": __version__,
              "application_id": APPLICATION_ID, "frozen": bool(getattr(sys, "frozen", False)),
              "python": sys.version.split()[0], "npcap_available": npcap_available(),
              "modules": modules, "resources": resources, "capture_started": False, "gui_started": False}
    atomic_write(Path(output), json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    return report
