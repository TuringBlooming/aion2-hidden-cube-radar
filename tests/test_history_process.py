"""Actual offline service restarts; never start the capture module or game."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.history_store import WriterLock
from test_history_store import record, history

ROOT = Path(__file__).resolve().parents[1] / "src/aion2_redbag_map"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@unittest.skipUnless(os.name == "nt", "Windows profile and desktop-launch persistence")
class ProcessPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, USERPROFILE=str(self.root / "profile"),
                        LOCALAPPDATA=str(self.root / "profile" / "AppData" / "Local"), PYTHONUTF8="1")
        self.processes = []

    def tearDown(self):
        for process, port in self.processes:
            if process.poll() is None:
                self.stop(process, port)
            process.communicate(timeout=3)
        self.tmp.cleanup()

    def install(self, name, rows):
        directory = self.root / name
        directory.mkdir()
        package = directory / "aion2_redbag_map"
        shutil.copytree(ROOT, package, ignore=shutil.ignore_patterns('__pycache__'))
        (package / "data").mkdir()
        (package / "data" / "history.json").write_text(json.dumps(history(*rows)), encoding="utf-8")
        return directory

    def launch(self, directory, *extra):
        port = free_port()
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "aion2_redbag_map.app",
                                    "--no-browser", "--no-capture", "--empty", "--port", str(port), *extra],
                                   env={**self.env, 'PYTHONPATH': str(directory)}, cwd=str(directory), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.processes.append((process, port))
        return process, port

    def state(self, process, port):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(f"offline server exited: {process.communicate()[1].decode('utf-8', errors='replace')}")
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/state", timeout=.5) as response:
                    return json.load(response)
            except OSError:
                time.sleep(.05)
        self.fail("offline server did not become ready")

    def stop(self, process, port):
        try:
            state = self.state(process, port)
            request = Request(f"http://127.0.0.1:{port}/api/control", data=b'{"action":"shutdown"}',
                              headers={"X-Redbag-Key": state["control_token"], "Content-Type": "application/json"})
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
            process.wait(timeout=8)
        finally:
            if process.poll() is None:
                process.terminate()  # Only this test's own known child, on failure.
                process.wait(timeout=3)

    def test_different_unpacked_copies_share_records_after_two_service_restarts(self):
        first = self.install("original-tool", [record(), record("old-only", x=120000)])
        second = self.install("freshly-unpacked-tool", [record(), record("new-only", map_id=1010)])
        process, port = self.launch(first)
        initial = self.state(process, port)
        self.assertEqual(initial["storage"]["record_count"], 2)
        self.assertTrue(initial["storage"]["readonly"])
        self.stop(process, port)
        process, port = self.launch(second)
        merged = self.state(process, port)
        self.assertEqual(merged["storage"]["history_file"], initial["storage"]["history_file"])
        self.assertEqual({row["id"] for row in merged["records"]}, {"bag-A", "old-only", "new-only"})
        self.stop(process, port)
        process, port = self.launch(first)
        restarted = self.state(process, port)
        self.assertEqual(len(restarted["records"]), 3)
        self.assertEqual(restarted["alert_seq"], 0)
        self.assertEqual(restarted["mode"], "offline")
        restored = next(row for row in restarted["records"] if row["id"] == "bag-A")
        for key in ("x", "y", "z", "first_seen", "opened_at"):
            self.assertEqual(restored[key], record()[key])
        self.assertEqual(restored["refresh_anchor_at"], record()["last_seen"])

    def test_other_process_cannot_modify_locked_history_even_on_another_port(self):
        directory = self.install("locked-tool", [record()])
        data = directory / "aion2_redbag_map/data"
        original = (data / "history.json").read_bytes()
        with WriterLock(data):
            process, _port = self.launch(directory, "--data-dir", str(data))
            _out, error = process.communicate(timeout=8)
            self.assertEqual(process.returncode, 1)
            self.assertIn("已有工具正在保存此存档", error.decode("utf-8"))
            self.assertEqual((data / "history.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
