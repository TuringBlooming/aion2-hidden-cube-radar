"""Version the local backpack archive without touching its writer or the game.

Examples:
    python git_archive.py --data-dir <archive-directory> --once
    python git_archive.py --data-dir <archive-directory> --watch
    python git_archive.py --data-dir <archive-directory> --watch --parent-pid <tool-pid> --wait-for-lock 10
    python git_archive.py --data-dir <archive-directory> --stop

The watcher commits at most once per 30 seconds while changes keep arriving.
It reads complete history.json snapshots, never writes history.json, and never
contacts a remote. A separate OS lock makes repeated launches harmless.
During a tool restart, a parent-bound watcher can wait up to 15 seconds for the
previous owner to finish. It never takes a held lock or stops another watcher.
"""
from __future__ import annotations

import argparse
from contextlib import AbstractContextManager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from .history_store import atomic_write, decode_history

OWNER = "AION2 RedBag Map local history archive v1\n"
IGNORE = b"# Only the backpack archive belongs in this repository.\n*\n!.gitignore\n!.gitattributes\n!history.json\n"
ATTRIBUTES = b"history.json -text\n.gitignore text eol=lf\n.gitattributes text eol=lf\n"
TRACKED = {".gitattributes", ".gitignore", "history.json"}
BRANCH = "refs/heads/main"
POLL_SECONDS = 2
COMMIT_INTERVAL = 30
GIT_TIMEOUT = 12
MAX_LOCK_WAIT_SECONDS = 15
LOCK_RETRY_SECONDS = .1


class ArchiveBusy(OSError):
    pass


class ArchiveError(OSError):
    pass


class ArchiveLock(AbstractContextManager):
    def __init__(self, directory):
        self.directory = Path(directory)
        self.stream = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stream = (self.directory / "git-archive.lock").open("a+b")
        if self.stream.seek(0, os.SEEK_END) == 0:
            self.stream.write(b"\0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise ArchiveBusy("本地 Git 存档已在运行。") from exc
        return self

    def __exit__(self, *_args):
        if self.stream:
            self.stream.close()
            self.stream = None


class ParentLifetime(AbstractContextManager):
    """Observe only the explicit tool PID; hold a Windows handle against PID reuse."""
    def __init__(self, pid=None):
        self.pid = pid
        self.handle = None
        self.kernel = None

    def __enter__(self):
        if self.pid is not None and os.name == "nt":
            import ctypes
            from ctypes import wintypes
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.kernel.OpenProcess.restype = wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype = wintypes.DWORD
            self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel.CloseHandle.restype = wintypes.BOOL
            # SYNCHRONIZE alone: no VM, modules, executable path, or other process queries.
            self.handle = self.kernel.OpenProcess(0x00100000, False, self.pid)
        return self

    def alive(self):
        if self.pid is None:
            return True
        if os.name == "nt":
            return bool(self.handle and self.kernel.WaitForSingleObject(self.handle, 0) == 258)
        try:
            os.kill(self.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return False

    def __exit__(self, *_args):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _safe_snapshot(raw):
    data = decode_history(raw)
    forbidden = {"control_token", "access_token", "refresh_token", "api_key", "password",
                 "authorization", "raw_packet", "raw_packets", "capture", "chat", "players"}

    def inspect(value):
        if isinstance(value, dict):
            if any(str(key).casefold() in forbidden for key in value):
                raise ValueError("history contains fields outside the backpack archive")
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(data)
    if not all(isinstance(row, dict) and isinstance(row.get("id"), str) for row in data["records"]):
        raise ValueError("history contains invalid records")
    return data


class ArchiveGit:
    def __init__(self, directory):
        self.directory = Path(directory).expanduser().resolve()
        self.path = self.directory / "history.json"
        self.status_path = self.directory / "git-status.json"
        self.stop_path = self.directory / "git-archive.stop"
        self.git = shutil.which("git")
        self.last_digest = None
        self._state = {"repository": str(self.directory), "active": False,
                       "last_commit": None, "last_commit_at": None, "pending": False,
                       "error": None, "checked_at": None, "pid": None}

    def status(self):
        return dict(self._state)

    def _publish(self):
        self._state["checked_at"] = time.time()
        try:
            atomic_write(self.status_path, json.dumps(self._state, ensure_ascii=False).encode("utf-8"))
        except OSError:
            # A status failure must never cause the receiver's main archive to fail.
            pass

    def _git(self, *arguments, raw=None, allowed=(0,)):
        if self.git is None:
            self.git = shutil.which("git")
        if self.git is None:
            raise ArchiveError("未找到 Git；背包主存档仍会正常保存。")
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_ATTR_NOSYSTEM="1")
        command = [self.git, "-C", str(self.directory), "-c", "core.autocrlf=false",
                   "-c", "core.quotePath=false", "-c", "commit.gpgsign=false",
                   "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                   "-c", "user.name=AION2 Backpack Archive", "-c", "user.email=archive@localhost",
                   *arguments]
        try:
            result = subprocess.run(command, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=GIT_TIMEOUT, env=env,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ArchiveError(f"本地 Git 存档失败：{type(exc).__name__}") from exc
        if result.returncode not in allowed:
            # Do not echo arbitrary config or file content from stderr into the UI.
            raise ArchiveError(f"本地 Git 操作 {arguments[0]} 失败（{result.returncode}）；主存档未改动。")
        return result.returncode, result.stdout

    def _ensure_repository(self):
        git_directory = self.directory / ".git"
        owner_path = git_directory / "redbag-archive-owner"
        if git_directory.exists():
            if (not git_directory.is_dir() or git_directory.is_symlink()
                    or not owner_path.is_file() or owner_path.read_text("utf-8") != OWNER):
                raise ArchiveError("此目录已有其他 Git 仓库，未改动它；请选择独立存档目录。")
        else:
            if any((parent / ".git").exists() for parent in self.directory.parents):
                raise ArchiveError("存档目录位于其他 Git 仓库内，未改动上层仓库。")
            for name, expected in ((".gitignore", IGNORE), (".gitattributes", ATTRIBUTES)):
                path = self.directory / name
                if path.exists() and path.read_bytes() != expected:
                    raise ArchiveError(f"已有 {name} 与存档规则不同，原文件未改动。")
            self._git("init", "--initial-branch=main", "--template=", ".")
            atomic_write(owner_path, OWNER.encode("utf-8"))
        for name, expected in ((".gitignore", IGNORE), (".gitattributes", ATTRIBUTES)):
            path = self.directory / name
            if not path.exists():
                atomic_write(path, expected)
            elif path.read_bytes() != expected:
                raise ArchiveError(f"{name} 已被修改，暂停自动版本记录以保留该修改。")
        _, branch = self._git("symbolic-ref", "-q", "HEAD")
        if branch.decode("ascii").strip() != BRANCH:
            raise ArchiveError("存档仓库当前分支已改变，暂停自动提交。")

    def _head(self):
        code, output = self._git("rev-parse", "--verify", "HEAD", allowed=(0, 128))
        return output.decode("ascii").strip() if code == 0 else None

    def _refresh_commit_status(self, head):
        if head:
            _, stamp = self._git("show", "-s", "--format=%ct", head)
            self._state.update(last_commit=head, last_commit_at=int(stamp.strip()))

    def _commit_snapshot(self, raw, data):
        self._ensure_repository()
        head = self._head()
        if head:
            _, existing = self._git("ls-tree", "-r", "--name-only", head)
            names = set(existing.decode("utf-8").splitlines())
            if not names <= TRACKED:
                raise ArchiveError("存档分支包含其他文件，未覆盖已有提交。")
        blobs = {}
        for name, content in ((".gitattributes", ATTRIBUTES), (".gitignore", IGNORE), ("history.json", raw)):
            _, object_id = self._git("hash-object", "-w", "--stdin", raw=content)
            blobs[name] = object_id.decode("ascii").strip()
        tree_input = "".join(f"100644 blob {blobs[name]}\t{name}\n" for name in sorted(blobs)).encode("utf-8")
        _, tree_bytes = self._git("mktree", raw=tree_input)
        tree = tree_bytes.decode("ascii").strip()
        if head:
            _, old_tree = self._git("rev-parse", head + "^{tree}")
            if tree == old_tree.decode("ascii").strip():
                self._refresh_commit_status(head)
                self._update_archive_index(blobs)
                return False
        arguments = ["commit-tree", tree]
        if head:
            arguments += ["-p", head]
        arguments += ["-m", f"Save backpack history ({len(data['records'])} records)"]
        _, commit = self._git(*arguments)
        commit = commit.decode("ascii").strip()
        # Compare-and-swap refuses a competing manual branch update.
        self._git("update-ref", BRANCH, commit, head or "0" * len(commit))
        self._update_archive_index(blobs)
        self._refresh_commit_status(commit)
        return True

    def _update_archive_index(self, blobs):
        # Never construct the commit from the shared index: unrelated staged files
        # remain staged locally and cannot be included in an automatic commit.
        for name, object_id in blobs.items():
            self._git("update-index", "--add", "--cacheinfo", "100644", object_id, name)

    def _sync_unlocked(self):
        try:
            raw = self.path.read_bytes()
            data = _safe_snapshot(raw)
            committed = self._commit_snapshot(raw, data)
            self.last_digest = hashlib.sha256(raw).digest()
            # A concurrent atomic save is left pending for the next batch.
            self._state["pending"] = hashlib.sha256(self.path.read_bytes()).digest() != self.last_digest
            self._state["error"] = None
            return committed
        except FileNotFoundError:
            self._state.update(error="等待背包主存档生成。", pending=False)
        except (OSError, ValueError, UnicodeError, TypeError, RecursionError) as exc:
            self._state.update(error=str(exc) if isinstance(exc, ArchiveError) else f"Git 存档未更新：{type(exc).__name__}；主存档未改动。",
                               pending=True)
        finally:
            self._publish()
        return False

    def sync_once(self):
        with ArchiveLock(self.directory):
            self._sync_unlocked()
            return self.status()

    def watch(self, poll_seconds=POLL_SECONDS, commit_interval=COMMIT_INTERVAL, parent_pid=None,
              wait_for_lock=0):
        if not 0 <= wait_for_lock <= MAX_LOCK_WAIT_SECONDS:
            raise ValueError("等待存档锁的时间必须在 0 到 15 秒之间。")
        if wait_for_lock and parent_pid is None:
            raise ValueError("等待存档锁需要指定父工具进程。")
        with ParentLifetime(parent_pid) as parent, ExitStack() as held:
            deadline = time.monotonic() + wait_for_lock
            attempted = False
            while True:
                # Never touch an existing owner's status or stop marker while
                # waiting. A dead new parent must not leave an orphan watcher.
                if not parent.alive():
                    return
                if attempted and time.monotonic() >= deadline:
                    raise ArchiveBusy("本地 Git 存档已在运行。")
                attempted = True
                try:
                    held.enter_context(ArchiveLock(self.directory))
                    break
                except ArchiveBusy:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(LOCK_RETRY_SECONDS, remaining))
            if not parent.alive():
                return
            self.stop_path.unlink(missing_ok=True)
            self._state.update(active=True, pid=os.getpid())
            self._sync_unlocked()
            last_attempt = time.monotonic()
            try:
                while not self.stop_path.exists() and parent.alive():
                    try:
                        raw_digest = hashlib.sha256(self.path.read_bytes()).digest()
                        self._state["pending"] = raw_digest != self.last_digest
                    except OSError:
                        self._state["pending"] = True
                    if ((self._state["pending"] or self._state["error"])
                            and time.monotonic() - last_attempt >= commit_interval):
                        self._sync_unlocked()
                        last_attempt = time.monotonic()
                    self._publish()
                    time.sleep(poll_seconds)
            except KeyboardInterrupt:
                pass
            finally:
                self._sync_unlocked()
                self._state.update(active=False, pid=None)
                self._publish()
                self.stop_path.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="AION2 背包存档的本地 Git 版本记录")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / "Documents" / "AION2-RedBag-Map" / "data")
    parser.add_argument("--parent-pid", type=int, help="只在指定地图工具进程运行期间记录版本")
    parser.add_argument("--wait-for-lock", type=float, metavar="SECONDS",
                        help="随父工具启动时等待旧存档进程释放锁（0–15 秒，默认 0）")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--watch", action="store_true")
    mode.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    if args.parent_pid is not None and (args.parent_pid <= 0 or not args.watch):
        parser.error("--parent-pid 必须为正整数，且只能与 --watch 一起使用")
    if args.wait_for_lock is not None:
        if not args.watch or args.parent_pid is None:
            parser.error("--wait-for-lock 只能与 --watch 和 --parent-pid 一起使用")
        if not 0 <= args.wait_for_lock <= MAX_LOCK_WAIT_SECONDS:
            parser.error("--wait-for-lock 必须在 0 到 15 秒之间")
    archive = ArchiveGit(args.data_dir)
    if args.stop:
        atomic_write(archive.stop_path, b"stop\n")
        return 0
    try:
        if args.watch:
            archive.watch(parent_pid=args.parent_pid, wait_for_lock=args.wait_for_lock or 0)
        else:
            archive.sync_once()
    except ArchiveBusy:
        print("本地 Git 存档已在运行。", flush=True)
        return 0
    if args.once:
        print(json.dumps(archive.status(), ensure_ascii=False), flush=True)
    return 1 if archive.status()["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
