import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map import __version__
from aion2_redbag_map.__main__ import main
from aion2_redbag_map.desktop import DesktopWindow, run
from aion2_redbag_map.desktop_text import TEXT as DESKTOP_TEXT, text as desktop_text
from aion2_redbag_map.localization import SUPPORTED_LANGUAGES
from aion2_redbag_map.runtime import (APPLICATION_ID, DesktopError, LocalClient, NpcapMissing,
                                     NoRedirect, RuntimeController, ServiceCollision, worker_command)


def state(directory, pid=900, mode="offline", **changes):
    return {"app": "AION2 RedBag Map", "application_id": APPLICATION_ID, "version": __version__,
            "process_id": pid, "mode": mode, "paused": False, "control_token": "test-secret-not-for-logs",
            "storage": {"directory": str(directory), "record_count": 12},
            "capture": {"phase": "observing", "detail": "test"}, **changes}


class FakeProcess:
    def __init__(self, pid):
        self.pid, self.alive = pid, True
        self.waits = []
        self.terminated = self.killed = False

    def poll(self):
        return None if self.alive else 0

    def wait(self, timeout):
        self.waits.append(timeout)
        self.alive = False
        return 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.killed = True
        self.alive = False


class FakeClient:
    def __init__(self, directory, value=None):
        self.directory = directory
        self.value = value
        self.url = "http://127.0.0.1:49173"
        self.controls = []

    def snapshot(self):
        return copy.deepcopy(self.value)

    def control(self, action, *, expected_pid=None, language=None):
        if expected_pid is not None and self.value["process_id"] != expected_pid:
            raise ServiceCollision("replacement")
        self.controls.append((action, expected_pid))
        if action == "shutdown":
            self.value = None
        elif action == "language":
            self.value["language"] = language
        else:
            self.value["paused"] = action == "pause"
        return {"ok": True}


class FakeFactory:
    def __init__(self, client):
        self.client, self.calls, self.processes = client, [], []
        self.collision = False

    def __call__(self, command, **options):
        self.calls.append((command, options))
        process = FakeProcess(1000 + len(self.calls))
        self.processes.append(process)
        kind = command[command.index("--worker") + 1]
        if kind == "service":
            self.client.value = state(self.client.directory, pid=9999 if self.collision else process.pid,
                                      mode="offline" if "--no-capture" in command else "live")
        elif kind == "overlay":
            (self.client.directory / "overlay-status.json").write_text(json.dumps(
                {"pid": process.pid, "running": True, "api": self.client.url + "/api/state"}), encoding="utf-8")
        return process


class DesktopControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Windows runners may expose Temp through an 8.3 path alias.
        self.directory = Path(self.temporary.name).resolve()
        self.client = FakeClient(self.directory)
        self.factory = FakeFactory(self.client)
        self.controller = RuntimeController(49173, self.directory, client=self.client,
                                            process_factory=self.factory, driver_check=lambda: True, sleep=lambda _s: None)

    def test_default_controller_does_not_start_any_worker_or_capture(self):
        current = self.controller.status()
        self.assertFalse(current["connected"])
        self.assertEqual(self.factory.calls, [])
        self.assertEqual(self.client.controls, [])
        self.assertNotIn("token", json.dumps(current))
        self.controller.close()
        self.assertEqual(self.factory.calls, [])

    def test_worker_commands_are_exact_and_never_use_a_shell(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(worker_command("service", ["--data-dir", "a path"]),
                             [sys.executable, "-m", "aion2_redbag_map", "--worker", "service", "--data-dir", "a path"])
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(worker_command("overlay"), [sys.executable, "--worker", "overlay"])
        with self.assertRaises(ValueError):
            worker_command("unrelated-program")
        with self.assertRaises(TypeError):
            worker_command("service", "--port")

    def test_history_is_read_only_then_switches_owned_service_to_live(self):
        self.controller.ensure_history()
        history_process = self.controller.service
        args, options = self.factory.calls[0]
        self.assertIn("--no-capture", args)
        self.assertIn("--read-only", args)
        self.assertIn(str(self.directory), args)
        self.assertNotIn("shell", options)
        self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.controller.start_receiving()
        self.assertEqual(self.client.controls, [("shutdown", history_process.pid)])
        self.assertFalse(history_process.alive)
        self.assertEqual(self.client.value["mode"], "live")
        self.assertNotIn("--no-capture", self.factory.calls[1][0])
        self.controller.stop_receiving()
        self.assertTrue(self.client.value["paused"])
        self.controller.start_receiving()
        self.assertFalse(self.client.value["paused"])
        live_process = self.controller.service
        self.controller.close()
        self.assertEqual(self.client.controls[-1], ("shutdown", live_process.pid))
        self.assertFalse(live_process.terminated)

    def test_missing_npcap_blocks_live_without_starting_workers_but_allows_history(self):
        self.controller.driver_check = lambda: False
        with self.assertRaises(NpcapMissing):
            self.controller.start_receiving()
        self.assertEqual(self.factory.calls, [])
        self.controller.ensure_history()
        self.assertEqual(self.client.value["mode"], "offline")
        self.assertIn("--no-capture", self.factory.calls[0][0])
        self.controller.close()

    def test_failed_owned_receiver_can_be_restarted_without_touching_external_services(self):
        self.controller.start_receiving()
        old_process = self.controller.service
        self.client.value["capture"]["phase"] = "error"
        self.controller.start_receiving()
        self.assertEqual(self.client.controls, [("shutdown", old_process.pid)])
        self.assertNotEqual(self.controller.service.pid, old_process.pid)
        self.controller.close()

    def test_hung_worker_fallback_uses_only_retained_child_handle(self):
        process = Mock(pid=1001)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("owned worker", 4), 0]
        self.controller._finish_process(process)
        process.terminate.assert_called_once_with()
        process.kill.assert_not_called()
        self.assertEqual(self.client.controls, [])

    def test_reused_service_is_never_shutdown_on_desktop_exit(self):
        self.client.value = state(self.directory, mode="live")
        self.controller.start_receiving()
        self.controller.ensure_history()
        self.controller.close()
        self.assertEqual(self.client.controls, [])
        self.assertEqual(self.factory.calls, [])
        self.assertEqual(self.client.value["process_id"], 900)

    def test_foreign_history_service_is_not_replaced_to_start_live(self):
        self.client.value = state(self.directory)
        with self.assertRaises(DesktopError):
            self.controller.start_receiving()
        self.assertEqual(self.client.controls, [])
        self.assertEqual(self.factory.calls, [])

    def test_racing_service_is_not_mistaken_for_our_child(self):
        self.factory.collision = True
        with self.assertRaises(ServiceCollision):
            self.controller.ensure_history()
        self.assertEqual(self.client.value["process_id"], 9999)
        self.assertEqual(self.client.controls, [])
        self.assertFalse(self.factory.processes[0].alive)
        self.assertIsNone(self.controller.service)

    def test_replaced_endpoint_is_never_sent_shutdown(self):
        self.controller.ensure_history()
        own = self.controller.service
        self.client.value = state(self.directory, pid=8888)
        self.controller.close()
        self.assertFalse(own.alive)
        self.assertEqual(self.client.controls, [])
        self.assertEqual(self.client.value["process_id"], 8888)

    def test_owned_overlay_closes_with_its_stop_marker_and_reused_overlay_is_left_open(self):
        self.controller.show_overlay()
        overlay = self.controller.overlay
        self.assertIsNotNone(overlay)
        self.controller.show_overlay()
        self.assertEqual(len(self.factory.calls), 2)
        self.controller.close()
        stop = json.loads((self.directory / "overlay.stop").read_text("utf-8"))
        self.assertGreater(stop["requested_at"], 0)
        self.assertFalse(overlay.terminated)
        (self.directory / "overlay.stop").unlink()
        self.client.value = state(self.directory, pid=900)
        (self.directory / "overlay-status.json").write_text(json.dumps(
            {"pid": 9876, "running": True, "api": self.client.url + "/api/state"}), encoding="utf-8")
        controls_before = len(self.client.controls)
        # Closing ends a manager lifecycle; another manager may reuse that overlay.
        self.controller = RuntimeController(49173, self.directory, client=self.client,
                                            process_factory=self.factory, driver_check=lambda: True, sleep=lambda _s: None)
        self.controller.show_overlay()
        self.controller.close()
        self.assertEqual(len(self.client.controls), controls_before)
        self.assertEqual(len(self.factory.calls), 2)
        self.assertFalse((self.directory / "overlay.stop").exists())

    def test_overlay_connection_mismatch_is_reported_without_stop_marker(self):
        self.client.value = state(self.directory)
        (self.directory / "overlay-status.json").write_text(json.dumps(
            {"pid": 9876, "running": True, "api": "http://127.0.0.1:49174/api/state"}), encoding="utf-8")
        with self.assertRaises(ServiceCollision):
            self.controller.show_overlay()
        self.assertEqual(self.factory.calls, [])
        self.assertFalse((self.directory / "overlay.stop").exists())

    def test_map_opening_uses_only_local_url_without_control_token(self):
        with patch("aion2_redbag_map.runtime.webbrowser.open") as browser:
            self.controller.open_map()
        browser.assert_called_once_with(self.client.url)
        self.assertIn("--no-capture", self.factory.calls[0][0])
        self.controller.close()

    def test_worker_dispatch_preserves_arguments_and_restores_host_argv(self):
        original = sys.argv
        seen = []
        module = SimpleNamespace(main=lambda: seen.append(list(sys.argv)) or 7)
        with patch("aion2_redbag_map.__main__.importlib.import_module", return_value=module) as load:
            self.assertEqual(main(["--worker", "archive", "--once", "--data-dir", "a path"]), 7)
        load.assert_called_once_with("aion2_redbag_map.git_archive")
        self.assertEqual(seen[0][1:], ["--once", "--data-dir", "a path"])
        self.assertIs(sys.argv, original)

    def test_self_check_does_not_create_gui_or_processes(self):
        output = self.directory / "check.json"
        with patch("subprocess.Popen", side_effect=AssertionError("must not start a process")):
            with patch("tkinter.Tk", side_effect=AssertionError("must not create a window")):
                result = main(["--self-check", str(output)])
        report = json.loads(output.read_text("utf-8"))
        self.assertEqual(result, 0 if report["ok"] else 1)
        self.assertEqual(report["version"], __version__)
        self.assertFalse(report["capture_started"])
        self.assertFalse(report["gui_started"])
        self.assertEqual(report["resources"]["map_count"], 18)
        self.assertEqual(report["resources"]["candidate_count"], 3721)
        self.assertEqual(report["resources"]["bundled_map_images"], 339)

    def test_windowed_worker_supplies_and_closes_missing_standard_streams(self):
        used = []
        def worker():
            used.extend((sys.stdout, sys.stderr, sys.stdin))
            print("worker diagnostic", flush=True)
            sys.stderr.write("worker diagnostic\n")
            self.assertEqual(sys.stdin.read(), "")
        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None), patch.object(sys, "stdin", None):
            with patch("aion2_redbag_map.__main__.importlib.import_module", return_value=SimpleNamespace(main=worker)):
                main(["--worker", "service"])
            self.assertIsNone(sys.stdout)
            self.assertIsNone(sys.stderr)
            self.assertIsNone(sys.stdin)
        self.assertTrue(all(stream.closed for stream in used))


class LocalClientTests(unittest.TestCase):
    def test_language_control_is_validated_before_accessing_the_service(self):
        with tempfile.TemporaryDirectory() as directory:
            client = LocalClient(49173, Path(directory))
            with patch.object(client, "snapshot", return_value=state(Path(directory))) as snapshot:
                with patch.object(client, "_request", return_value={"ok": True}) as request:
                    client.control("language", language="ja", expected_pid=900)
                    body = json.loads(request.call_args.args[0].data)
                    self.assertEqual(body, {"action": "language", "language": "ja"})
                    snapshot.reset_mock()
                    for action, language in (("language", "unsupported"), ("pause", "en")):
                        with self.assertRaises(ValueError):
                            client.control(action, language=language)
                    snapshot.assert_not_called()

    def test_closed_loopback_port_returns_none_without_starting_a_process(self):
        # On Windows a refused localhost connection can take about two seconds.
        # Exercise the real refusal path so a shorter HTTP timeout cannot mask it.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            with patch("subprocess.Popen", side_effect=AssertionError("must not start a process")):
                self.assertIsNone(LocalClient(port, Path(directory)).snapshot())

    def test_identity_and_directory_must_match_before_control(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = LocalClient(49173, root)
            for invalid in ({"app": "other"}, state(root, application_id="old-map"),
                            state(root / "another"), state(root, process_id=True)):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ServiceCollision):
                        client.validate(invalid)
            self.assertEqual(client.validate(state(root))["process_id"], 900)
            with self.assertRaises(ServiceCollision):
                NoRedirect().redirect_request(None, None, None, None, None)

    def test_loopback_http_ignores_proxies_sends_token_and_rejects_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calls = []
            class Handler(BaseHTTPRequestHandler):
                redirect = False
                def log_message(self, *_args):
                    pass
                def do_GET(self):
                    calls.append(("GET", self.path))
                    if self.redirect:
                        self.send_response(302)
                        self.send_header("Location", "/should-not-follow")
                        self.end_headers()
                        return
                    raw = json.dumps(state(root)).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    calls.append(("POST", self.path, self.headers.get("X-Redbag-Key"), self.headers.get("Origin"), body))
                    raw = b'{"ok": true}'
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9"}):
                    client = LocalClient(server.server_port, root)
                    self.assertEqual(client.snapshot()["process_id"], 900)
                    client.control("pause", expected_pid=900)
                post = next(call for call in calls if call[0] == "POST")
                self.assertEqual(post[1], "/api/control")
                self.assertEqual(post[2], "test-secret-not-for-logs")
                self.assertEqual(post[3], client.url)
                self.assertEqual(post[4], {"action": "pause"})
                with self.assertRaises(ServiceCollision):
                    client.control("shutdown", expected_pid=901)
                self.assertEqual(len([call for call in calls if call[0] == "POST"]), 1)
                Handler.redirect = True
                with self.assertRaises(ServiceCollision):
                    client.snapshot()
                self.assertFalse(any(call[1] == "/should-not-follow" for call in calls))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)


class DesktopLanguageTests(unittest.TestCase):
    def test_offline_preference_survives_restart_without_touching_history_or_starting_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = root / "history.json"
            original = b'{"schema":1,"language":"ja","records":[{"id":"keep-me"}]}'
            history.write_bytes(original)
            os.utime(history, (1, 1))
            client = FakeClient(root)
            factory = Mock(side_effect=AssertionError("language must not start workers"))
            controller = RuntimeController(data_dir=root, client=client, process_factory=factory)
            self.assertEqual(controller.language, "ja")
            controller.set_language("en")
            restarted = RuntimeController(data_dir=root, client=client, process_factory=factory)
            self.assertEqual(restarted.status()["language"], "en")
            self.assertEqual(history.read_bytes(), original)
            factory.assert_not_called()
            self.assertEqual(client.controls, [])
            history.write_text('{"language":"ko"}', encoding="utf-8")
            os.utime(history, (2_000_000_000, 2_000_000_000))
            self.assertEqual(RuntimeController(data_dir=root, client=client).language, "ko")

    def test_live_changes_use_authenticated_control_and_external_changes_are_remembered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(root, state(root, language="zh-CN", mode="live"))
            controller = RuntimeController(data_dir=root, client=client)
            controller.set_language("ko")
            self.assertEqual(client.controls, [("language", 900)])
            self.assertEqual(client.value["language"], "ko")
            self.assertFalse(client.value["paused"])
            client.value["language"] = "ja"
            self.assertEqual(controller.status()["language"], "ja")
            self.assertEqual(json.loads((root / "desktop-settings.json").read_text())["language"], "ja")
            self.assertNotIn("control_token", controller.status())

    def test_saved_offline_choice_reaches_a_new_history_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(root)
            factory = FakeFactory(client)
            controller = RuntimeController(data_dir=root, client=client, process_factory=factory, sleep=lambda _: None)
            controller.set_language("en")
            result = controller.ensure_history()
            self.assertEqual(result["language"], "en")
            self.assertIn("--no-capture", factory.calls[0][0])
            self.assertEqual(client.controls, [("language", factory.processes[0].pid)])
            controller.close()

    def test_failed_control_does_not_persist_an_unapplied_choice(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(root, state(root, language="zh-CN"))
            controller = RuntimeController(data_dir=root, client=client)
            with patch.object(client, "control", side_effect=DesktopError("rejected")):
                with self.assertRaises(DesktopError):
                    controller.set_language("ko")
            self.assertEqual(controller.language, "zh-CN")
            self.assertFalse((root / "desktop-settings.json").exists())

    def test_preference_write_error_does_not_hide_live_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(root, state(root, language="en", mode="live"))
            controller = RuntimeController(data_dir=root, client=client)
            with patch("aion2_redbag_map.runtime.atomic_write", side_effect=PermissionError("read only")):
                status = controller.status()
            self.assertTrue(status["connected"])
            self.assertEqual(status["language"], "en")
            self.assertTrue(status["language_save_error"])
            self.assertFalse(controller.status()["language_save_error"])

    @unittest.skipUnless(os.name == "nt", "Native Windows font and DPI layout")
    def test_native_language_switch_and_layout_at_supported_dpi_scales(self):
        import tkinter as tk
        from aion2_redbag_map.ui_theme import enable_dpi_awareness
        enable_dpi_awareness()
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for scale in (1, 1.25, 1.5, 2):
                with self.subTest(scale=scale):
                    root = tk.Tk()
                    root.withdraw()
                    root.tk.call("tk", "scaling", scale * 96 / 72)
                    client = FakeClient(folder, state(folder, language="zh-CN", mode="live"))
                    controller = RuntimeController(data_dir=folder, client=client, driver_check=lambda: True)
                    try:
                        with patch("tkinter.Tk", return_value=root), patch("aion2_redbag_map.ui_theme.ui_scale", return_value=scale):
                            window = DesktopWindow(controller)
                        for timer in root.tk.call("after", "info"):
                            root.after_cancel(timer)
                        root.geometry(f"{window.px(760)}x{window.px(640)}-20000-20000")
                        root.attributes("-alpha", 0)
                        root.deiconify()
                        root.update()
                        for language in SUPPORTED_LANGUAGES:
                            client.value["language"] = language
                            window.apply_status(controller.status())
                            root.update_idletasks()
                            self.assertEqual(window.language_select.current(), SUPPORTED_LANGUAGES.index(language))
                            self.assertEqual(root.title(), desktop_text("title", language))
                            self.assertEqual(window.start_button.cget("text"), desktop_text("start", language))
                            for widget, key in window.localized_widgets:
                                self.assertEqual(widget.cget("text"), DESKTOP_TEXT[key][SUPPORTED_LANGUAGES.index(language)])
                                self.assertLessEqual(widget.winfo_reqwidth(), widget.winfo_width() + 2, (scale, language, key, "clipped"))
                                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), root.winfo_rooty() + root.winfo_height(), (scale, language, key, "outside window"))
                            self.assertFalse(hasattr(window, "driver_link"))
                        window.language_select.current(1)
                        with patch.object(window, "perform") as perform:
                            window.language_select.event_generate("<<ComboboxSelected>>")
                            root.update()
                            perform.assert_called_once()
                            self.assertEqual(window.start_button.cget("text"), desktop_text("start", "en"))
                            window.apply_status(controller.status())
                            self.assertEqual(window.language, "en", "An old status must not undo the pending choice")
                            perform.call_args.args[0]()
                        window.pending_language = None
                        window.apply_status(controller.status())
                        self.assertEqual(window.language, "en")
                        self.assertEqual(client.value["language"], "en")
                        self.assertFalse(client.value["paused"])
                    finally:
                        root.destroy()


class ExplicitStartupTests(unittest.TestCase):
    def window(self):
        window = DesktopWindow.__new__(DesktopWindow)
        window.root = Mock()
        window.controller = Mock()
        window.perform = Mock()
        window.closing = False
        return window

    def test_default_entry_forwards_idle_flags(self):
        with patch("aion2_redbag_map.desktop.run", return_value=0) as desktop:
            self.assertEqual(main([]), 0)
        desktop.assert_called_once_with(18726, None, start_receiving=False, show_overlay=False, minimized=False)
        window = self.window()
        window.schedule_startup()
        window.root.iconify.assert_not_called()
        window.root.after_idle.assert_not_called()
        window.perform.assert_not_called()
        self.assertEqual(window.controller.mock_calls, [])

    def test_explicit_flags_forward_through_entry_and_run(self):
        with patch("aion2_redbag_map.desktop.run", return_value=0) as desktop:
            main(["--port", "49173", "--data-dir", "isolated", "--start-receiving", "--show-overlay", "--minimized"])
        desktop.assert_called_once_with(49173, Path("isolated"), start_receiving=True, show_overlay=True, minimized=True)
        with patch("aion2_redbag_map.desktop.RuntimeController") as controller:
            with patch("aion2_redbag_map.desktop.DesktopWindow") as window:
                run(49173, Path("isolated"), start_receiving=True, show_overlay=True, minimized=True)
        controller.assert_called_once_with(49173, Path("isolated"))
        window.assert_called_once_with(controller.return_value, start_receiving=True, show_overlay=True, minimized=True)
        window.return_value.run.assert_called_once_with()

    def test_requested_start_is_one_managed_task_in_receive_then_overlay_order(self):
        window = self.window()
        window.schedule_startup(start_receiving=True, show_overlay=True, minimized=True)
        window.root.iconify.assert_called_once_with()
        window.root.after_idle.assert_called_once()
        self.assertEqual(window.controller.mock_calls, [])
        window.root.after_idle.call_args.args[0]()
        window.perform.assert_called_once()
        action = window.perform.call_args.args[0]
        action()
        self.assertEqual([call[0] for call in window.controller.mock_calls], ["start_receiving", "show_overlay"])

    def test_overlay_only_does_not_implicitly_start_live_and_closing_cancels_pending_steps(self):
        window = self.window()
        window.start_requested(False, True)
        window.perform.call_args.args[0]()
        window.controller.start_receiving.assert_not_called()
        window.controller.show_overlay.assert_called_once_with()
        window = self.window()
        window.schedule_startup(start_receiving=True, show_overlay=True)
        window.closing = True
        window.root.after_idle.call_args.args[0]()
        window.perform.assert_not_called()
        window = self.window()
        window.start_requested(True, True)
        action = window.perform.call_args.args[0]
        window.controller.start_receiving.side_effect = lambda: setattr(window, "closing", True)
        action()
        window.controller.show_overlay.assert_not_called()

    def test_failed_live_start_does_not_open_overlay_as_if_start_succeeded(self):
        window = self.window()
        window.start_requested(True, True)
        window.controller.start_receiving.side_effect = DesktopError("start failed")
        with self.assertRaises(DesktopError):
            window.perform.call_args.args[0]()
        window.controller.show_overlay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
