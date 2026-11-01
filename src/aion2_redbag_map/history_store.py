"""Durable local history, shared by installations belonging to one Windows user."""
from __future__ import annotations

from contextlib import AbstractContextManager
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time


def data_directory(root, args):
    if args.data_dir:
        return Path(args.data_dir).expanduser().resolve()
    if args.replay:
        return Path(root) / "replay-data"
    # LocalAppData can be redirected into a packaged host's private LocalCache.
    # A profile Documents path stays the same from Explorer and packaged hosts.
    return Path.home() / "Documents" / "AION2-RedBag-Map" / "data"


def legacy_histories(root):
    paths = [Path(root) / "data" / "history.json", Path(root) / "history-data" / "history.json"]
    if os.environ.get("LOCALAPPDATA"):
        paths.append(Path(os.environ["LOCALAPPDATA"]) / "AION2-RedBag-Map" / "data" / "history.json")
    return paths


def decode_history(raw):
    def invalid_number(_value):
        raise ValueError("non-finite history number")
    data = json.loads(raw.decode("utf-8-sig"), parse_constant=invalid_number)
    if (not isinstance(data, dict) or data.get("schema") != 1
            or not isinstance(data.get("records"), list)
            or not isinstance(data.get("events", []), list)):
        raise ValueError("unsupported history format")
    return data


def atomic_write(path, raw):
    """Never replace the previous file until the complete new file is flushed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _stamp(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0 else 0


def merge_histories(primary, additions):
    """Keep first discovery/open facts, but use the latest update for the timer."""
    sources = ([primary] if primary is not None else []) + list(additions)
    result = dict(sources[0]) if sources else {"schema": 1}
    records, events = {}, {}
    for source in sources:
        for row in source.get("records", []):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            old = records.get(row["id"])
            if old is None:
                copied = dict(row)
                anchor = max(_stamp(row.get(key)) for key in
                             ("last_seen", "opened_at", "received_at", "refresh_anchor_at", "first_seen"))
                if anchor:
                    copied["refresh_anchor_at"] = anchor
                records[row["id"]] = copied
                continue
            merged = dict(row if _stamp(row.get("last_seen")) > _stamp(old.get("last_seen")) else old)
            for key in ("first_seen", "received_at", "opened_at"):
                values = [_stamp(r.get(key)) for r in (old, row) if _stamp(r.get(key))]
                if values:
                    merged[key] = min(values)
            merged["refresh_anchor_at"] = max(_stamp(r.get(key)) for r in (old, row) for key in
                                               ("last_seen", "opened_at", "received_at", "refresh_anchor_at", "first_seen")) or None
            if old.get("status") == "opened" or row.get("status") == "opened":
                opened = old if old.get("status") == "opened" else row
                merged["status"] = "opened"
                if "reason" in opened:
                    merged["reason"] = opened["reason"]
            records[row["id"]] = merged
        for event in source.get("events", []):
            if isinstance(event, dict) and isinstance(event.get("id"), str):
                events.setdefault(event["id"], dict(event))
    result.update(schema=1, records=sorted(records.values(), key=lambda r: _stamp(r.get("last_seen"))),
                  events=sorted(events.values(), key=lambda e: _stamp(e.get("at")))[-300:])
    return result


class HistoryStore:
    def __init__(self, directory, readonly=False):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "history.json"
        self.previous = self.directory / "history.previous.json"
        self.backups = self.directory / "backups"
        self.readonly = readonly
        self.recovered_from = None
        self.error = None
        self.blocked = False
        self.last_saved_at = None
        self.disk_version = None

    def version(self):
        try:
            stat = self.path.stat()
            return stat.st_mtime_ns, stat.st_size
        except FileNotFoundError:
            return None

    def archive(self, raw, label):
        digest = hashlib.sha256(raw).hexdigest()[:20]
        target = self.backups / f"{label}-{digest}.json"
        if not target.exists():
            atomic_write(target, raw)
        return target

    def backup_paths(self):
        paths = [p for p in self.backups.glob("*.json") if not p.name.startswith("unreadable-")]
        if self.previous.exists():
            paths.append(self.previous)
        return sorted(paths, key=lambda p: p.stat().st_mtime_ns, reverse=True)

    def backup_histories(self):
        for path in self.backup_paths():
            try:
                yield path, decode_history(path.read_bytes())
            except (OSError, ValueError, UnicodeError):
                continue

    def load(self):
        self.error = None
        self.blocked = False
        self.disk_version = self.version()
        candidates = [self.path, *self.backup_paths()]
        failed = []
        recovered = []
        for path in candidates:
            if not path.exists():
                continue
            try:
                raw = path.read_bytes()
                data = decode_history(raw)
            except (OSError, ValueError, UnicodeError) as exc:
                failed.append(type(exc).__name__)
                if path == self.path and not self.readonly:
                    try:
                        self.archive(path.read_bytes(), "unreadable")
                    except OSError:
                        self.blocked = True
                continue
            self.last_saved_at = path.stat().st_mtime
            if path == self.path:
                return data
            if not recovered:
                self.recovered_from = str(path)
                self.last_saved_at = path.stat().st_mtime
            recovered.append(data)
        if recovered:
            self.last_saved_at = Path(self.recovered_from).stat().st_mtime
            return merge_histories(recovered[0], recovered[1:])
        if failed:
            self.blocked = True
            self.error = "历史记录未能读取，原文件已保留；请从备份恢复。"
        return None

    def save(self, data):
        if self.readonly:
            return
        if self.blocked:
            raise OSError("existing history must be recovered before saving")
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        decode_history(raw)
        if self.path.exists():
            old = self.path.read_bytes()
            try:
                decode_history(old)
            except (ValueError, UnicodeError):
                self.archive(old, "unreadable")
            else:
                atomic_write(self.previous, old)
        # Keep one independent snapshot per hour as well as the previous write.
        hourly = self.backups / time.strftime("history-%Y%m%d-%H.json")
        if not hourly.exists():
            atomic_write(hourly, raw)
        atomic_write(self.path, raw)
        self.disk_version = self.version()
        self.last_saved_at = self.path.stat().st_mtime
        self.error = None

    def backup_count(self):
        return int(self.previous.exists()) + sum(1 for p in self.backups.glob("*.json") if not p.name.startswith("unreadable-"))


def migrate_legacy(directory, paths):
    """Copy legacy archives, then merge. Originals are never moved or deleted."""
    store = HistoryStore(directory)
    primary = store.load()
    if store.blocked:
        raise OSError(store.error or "history recovery failed")
    additions = []
    for path in paths:
        path = Path(path)
        if path.resolve() == store.path or not path.exists():
            continue
        raw = path.read_bytes()
        try:
            data = decode_history(raw)
        except (ValueError, UnicodeError):
            store.archive(raw, "unreadable-legacy")
            continue
        store.archive(raw, "imported")
        additions.append(data)
    if additions:
        merged = merge_histories(primary, additions)
        if merged != primary or store.recovered_from:
            store.save(merged)
        return merged
    return primary


class HistoryInUse(OSError):
    pass


class WriterLock(AbstractContextManager):
    """Only one writer may own a shared archive, including across install copies."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.stream = None

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.stream = (self.directory / "history.lock").open("a+b")
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
            raise HistoryInUse("已有工具正在保存此存档，请使用已打开的地图。") from exc
        return self

    def __exit__(self, *_args):
        if self.stream:
            # Closing the handle releases the OS lock, including on abnormal exit.
            self.stream.close()
            self.stream = None
