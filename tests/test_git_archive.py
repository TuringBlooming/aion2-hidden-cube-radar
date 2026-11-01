import hashlib
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.git_archive import ArchiveGit, ArchiveLock, ArchiveBusy, main
from aion2_redbag_map.history_store import atomic_write


@unittest.skipUnless(shutil.which("git"), "local Git executable required")
class GitArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name) / "data"
        self.directory.mkdir()
        self.path = self.directory / "history.json"
        self.archive = ArchiveGit(self.directory)
        self.write(1)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, count, **extra):
        data = {"schema": 1, "records": [{"id": f"bag-{i}", "last_seen": 100 + i} for i in range(count)],
                "events": [], "language": "zh-CN", **extra}
        atomic_write(self.path, json.dumps(data, ensure_ascii=False).encode("utf-8"))
        return self.path.read_bytes()

    def git(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        return subprocess.run([shutil.which("git"), "-C", str(self.directory), *args], check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0).stdout

    def test_initial_commit_versions_exact_snapshot_and_only_three_files(self):
        original = self.path.read_bytes()
        (self.directory / "app.log").write_text("private diagnostic text")
        (self.directory / "history.previous.json").write_text("old backup")
        state = self.archive.sync_once()
        self.assertIsNone(state["error"])
        self.assertTrue(state["last_commit"])
        self.assertFalse(state["pending"])
        self.assertEqual(self.git("show", "HEAD:history.json"), original)
        self.assertEqual(set(self.git("ls-tree", "-r", "--name-only", "HEAD").decode().splitlines()),
                         {".gitignore", ".gitattributes", "history.json"})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.git("status", "--porcelain"), b"")
        self.assertEqual(self.git("remote"), b"")
        self.assertEqual(self.git("show", "-s", "--format=%an|%ae", "HEAD").strip(),
                         b"AION2 Backpack Archive|archive@localhost")

    def test_unchanged_content_does_not_commit_but_changed_content_does(self):
        first = self.archive.sync_once()["last_commit"]
        second = self.archive.sync_once()["last_commit"]
        self.assertEqual(first, second)
        changed = self.write(2)
        third = self.archive.sync_once()["last_commit"]
        self.assertNotEqual(first, third)
        self.assertEqual(self.git("rev-list", "--count", "HEAD").strip(), b"2")
        self.assertEqual(self.git("show", "HEAD:history.json"), changed)
        self.assertEqual(len(json.loads(self.git("show", "HEAD~1:history.json"))["records"]), 1)

    def test_invalid_or_sensitive_json_does_not_commit_or_change_main_file(self):
        first = self.archive.sync_once()["last_commit"]
        invalid = [b'{"schema":1,"records":[', b'{"schema":2,"records":[]}',
                   json.dumps({"schema": 1, "records": [], "control_token": "private"}).encode()]
        for raw in invalid:
            with self.subTest(raw=raw[:25]):
                atomic_write(self.path, raw)
                state = self.archive.sync_once()
                self.assertTrue(state["error"])
                self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), first)
                self.assertEqual(self.path.read_bytes(), raw)
        self.write(2)
        self.assertIsNone(self.archive.sync_once()["error"])

    def test_staged_unrelated_file_is_not_included_or_unstaged(self):
        self.archive.sync_once()
        extra = self.directory / "unrelated.txt"
        extra.write_text("manual staged work")
        self.git("add", "-f", "unrelated.txt")
        before = self.git("ls-files", "--stage", "unrelated.txt")
        self.write(2)
        self.assertIsNone(self.archive.sync_once()["error"])
        self.assertEqual(self.git("ls-files", "--stage", "unrelated.txt"), before)
        self.assertNotIn(b"unrelated.txt", self.git("ls-tree", "-r", "--name-only", "HEAD"))

    def test_atomic_update_during_commit_leaves_newer_file_pending(self):
        first = self.path.read_bytes()
        actual = self.archive._commit_snapshot

        def concurrent(raw, data):
            result = actual(raw, data)
            self.write(2)
            return result

        with patch.object(self.archive, "_commit_snapshot", side_effect=concurrent):
            state = self.archive.sync_once()
        self.assertTrue(state["pending"])
        self.assertEqual(self.git("show", "HEAD:history.json"), first)
        self.assertEqual(len(json.loads(self.path.read_text("utf-8"))["records"]), 2)
        self.assertFalse(self.archive.sync_once()["pending"])

    def test_other_repository_and_parent_repository_are_preserved(self):
        self.git("init")
        original = self.path.read_bytes()
        state = self.archive.sync_once()
        self.assertIn("其他 Git 仓库", state["error"])
        self.assertFalse((self.directory / ".git" / "redbag-archive-owner").exists())
        self.assertEqual(self.path.read_bytes(), original)
        nested = self.directory / "nested"
        nested.mkdir()
        atomic_write(nested / "history.json", original)
        state = ArchiveGit(nested).sync_once()
        self.assertIn("上层仓库", state["error"])
        self.assertFalse((nested / ".git").exists())

    def test_git_environment_cannot_redirect_objects_or_identity(self):
        external = Path(self.tmp.name) / "unrelated-git"
        with patch.dict(os.environ, {"GIT_DIR": str(external), "GIT_WORK_TREE": str(external),
                                     "GIT_INDEX_FILE": str(external / "index"),
                                     "GIT_AUTHOR_NAME": "wrong author", "GIT_CONFIG_COUNT": "1",
                                     "GIT_CONFIG_KEY_0": "alias.anything", "GIT_CONFIG_VALUE_0": "!echo unsafe"}):
            state = self.archive.sync_once()
        self.assertIsNone(state["error"])
        self.assertFalse(external.exists())
        self.assertEqual(self.git("show", "-s", "--format=%an", "HEAD").strip(), b"AION2 Backpack Archive")

    def test_missing_git_is_reported_without_changing_archive(self):
        original = self.path.read_bytes()
        self.archive.git = None
        with patch("aion2_redbag_map.git_archive.shutil.which", return_value=None):
            state = self.archive.sync_once()
        self.assertIn("未找到 Git", state["error"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.directory / ".git").exists())

    def test_duplicate_watcher_lock_and_once_cli_are_harmless(self):
        original = self.path.read_bytes()
        with ArchiveLock(self.directory):
            with self.assertRaises(ArchiveBusy):
                self.archive.sync_once()
            self.assertEqual(main(["--data-dir", str(self.directory), "--once"]), 0)
            with patch("aion2_redbag_map.git_archive.time.sleep", side_effect=AssertionError("default watch must not wait")):
                self.assertEqual(main(["--data-dir", str(self.directory), "--watch",
                                       "--parent-pid", str(os.getpid())]), 0)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIsNone(self.archive.sync_once()["error"])

    def test_lock_wait_cli_is_bounded_and_requires_watch_with_parent(self):
        base = ["--data-dir", str(self.directory)]
        invalid = [
            ["--once", "--wait-for-lock", "1"],
            ["--watch", "--wait-for-lock", "1"],
            *[["--watch", "--parent-pid", str(os.getpid()), "--wait-for-lock", value]
              for value in ("-1", "16", "nan", "inf")],
        ]
        for args in invalid:
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(base + args)
                self.assertEqual(caught.exception.code, 2)
        with patch.object(ArchiveGit, "watch") as watch:
            self.assertEqual(main(base + ["--watch", "--parent-pid", str(os.getpid()),
                                         "--wait-for-lock", "10"]), 0)
            watch.assert_called_once_with(parent_pid=os.getpid(), wait_for_lock=10)

    def start_parent(self):
        return subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def start_archive_worker(self, **kwargs):
        errors = []

        def run():
            try:
                self.archive.watch(**kwargs)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        return worker, errors

    def test_parent_bound_watcher_takes_over_only_after_old_owner_releases_lock(self):
        original = self.path.read_bytes()
        parent = self.start_parent()
        blocked = threading.Event()
        actual_enter = ArchiveLock.__enter__

        def observe_attempt(lock):
            try:
                return actual_enter(lock)
            except ArchiveBusy:
                blocked.set()
                raise

        worker = None
        try:
            with ArchiveLock(self.directory):
                with patch.object(ArchiveLock, "__enter__", observe_attempt):
                    worker, errors = self.start_archive_worker(parent_pid=parent.pid, wait_for_lock=2,
                                                               poll_seconds=.02)
                    self.assertTrue(blocked.wait(1))
                    self.assertTrue(worker.is_alive())
                    self.assertFalse((self.directory / ".git").exists())
                    self.assertFalse(self.archive.status_path.exists())
                    self.assertEqual(self.path.read_bytes(), original)
            deadline = time.monotonic() + 5
            while not self.archive.status()["last_commit"] and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(self.archive.status()["last_commit"])
            self.assertTrue(self.archive.status()["active"])
            self.assertEqual(self.git("show", "HEAD:history.json"), original)
        finally:
            parent.stdin.close()
            parent.wait(timeout=3)
            if worker is not None:
                worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.path.read_bytes(), original)

    def test_parent_exit_cancels_lock_wait_without_touching_existing_owner(self):
        original = self.path.read_bytes()
        status = b'{"active":true,"pid":123,"last_commit":"existing-owner"}'
        atomic_write(self.archive.status_path, status)
        atomic_write(self.archive.stop_path, b"existing stop marker\n")
        parent = self.start_parent()
        blocked = threading.Event()
        actual_enter = ArchiveLock.__enter__

        def observe_attempt(lock):
            try:
                return actual_enter(lock)
            except ArchiveBusy:
                blocked.set()
                raise

        worker = None
        try:
            with ArchiveLock(self.directory):
                with patch.object(ArchiveLock, "__enter__", observe_attempt):
                    worker, errors = self.start_archive_worker(parent_pid=parent.pid, wait_for_lock=3)
                    self.assertTrue(blocked.wait(1))
                    parent.stdin.close()
                    parent.wait(timeout=3)
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(errors, [])
                    self.assertFalse((self.directory / ".git").exists())
                    self.assertEqual(self.archive.status_path.read_bytes(), status)
                    self.assertEqual(self.archive.stop_path.read_bytes(), b"existing stop marker\n")
                    self.assertEqual(self.path.read_bytes(), original)
        finally:
            if not parent.stdin.closed:
                parent.stdin.close()
            parent.wait(timeout=3)
            if worker is not None:
                worker.join(timeout=5)

    def test_lock_wait_timeout_preserves_main_archive(self):
        original = self.path.read_bytes()
        with ArchiveLock(self.directory):
            started = time.monotonic()
            with self.assertRaises(ArchiveBusy):
                self.archive.watch(parent_pid=os.getpid(), wait_for_lock=.08)
            self.assertGreaterEqual(time.monotonic() - started, .08)
            self.assertLess(time.monotonic() - started, 1)
        self.assertFalse((self.directory / ".git").exists())
        self.assertFalse(self.archive.status_path.exists())
        self.assertEqual(self.path.read_bytes(), original)

    def test_watcher_batches_changes_and_flushes_on_requested_stop(self):
        worker = threading.Thread(target=self.archive.watch, kwargs={"poll_seconds": .02, "commit_interval": .15})
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not self.archive.status()["last_commit"] and time.monotonic() < deadline:
                time.sleep(.02)
            first = self.archive.status()["last_commit"]
            self.assertTrue(first)
            self.write(2)
            deadline = time.monotonic() + 5
            while self.archive.status()["last_commit"] == first and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertNotEqual(self.archive.status()["last_commit"], first)
            self.write(3)
        finally:
            atomic_write(self.archive.stop_path, b"stop\n")
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        state = json.loads(self.archive.status_path.read_text("utf-8"))
        self.assertFalse(state["active"])
        self.assertFalse(state["pending"])
        self.assertEqual(len(json.loads(self.git("show", "HEAD:history.json"))["records"]), 3)
        self.assertEqual(self.path.read_bytes(), self.git("show", "HEAD:history.json"))

    def test_parent_exit_flushes_pending_archive_and_stops_watcher(self):
        parent = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        worker = threading.Thread(target=self.archive.watch,
                                  kwargs={"poll_seconds": .02, "commit_interval": 30, "parent_pid": parent.pid})
        worker.start()
        try:
            deadline = time.monotonic() + 5
            while not self.archive.status()["last_commit"] and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(self.archive.status()["last_commit"])
            self.write(2)
        finally:
            parent.stdin.close()
            parent.wait(timeout=3)
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.archive.status()["active"])
        self.assertEqual(len(json.loads(self.git("show", "HEAD:history.json"))["records"]), 2)


if __name__ == "__main__":
    unittest.main()
