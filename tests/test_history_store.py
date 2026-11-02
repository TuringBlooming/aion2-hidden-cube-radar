import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.history_store import (data_directory, HistoryStore, HistoryInUse, WriterLock,
                           atomic_write, merge_histories, migrate_legacy)
from aion2_redbag_map.model import MapState

MAPS = [{"id": 1110, "name": "阿尔特盖德"}, {"id": 1010, "name": "Verteron"}]


def record(identity="bag-A", **changes):
    row = {"id": identity, "map_id": 1110, "entity_id": 42, "template_id": 1301199,
           "x": 106518.438, "y": 71755.414, "z": 4347, "status": "opened",
           "first_seen": 1792749938.926, "refresh_anchor_at": 1792749938.926,
           "received_at": 1792749938.926, "last_seen": 1792749952.829,
           "opened_at": 1792749952.829, "spawned_at": 1792749938682}
    row.update(changes)
    return row


def history(*rows, **preferences):
    return {"schema": 1, "selected_map_id": 1110, "language": "zh-CN", "sound": True,
            "records": list(rows), "events": [], **preferences}


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Use the same directory identity for short and long Windows paths.
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_install_location_does_not_change_default_and_replay_is_isolated(self):
        args = argparse.Namespace(data_dir=None, replay=None)
        with patch("aion2_redbag_map.history_store.Path.home", return_value=self.root / "profile"):
            first = data_directory(self.root / "original", args)
            second = data_directory(self.root / "new-unzip", args)
            self.assertEqual(first, second)
            self.assertEqual(first, self.root / "profile" / "Documents" / "AION2-RedBag-Map" / "data")
            with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "redirected-package-cache")}):
                self.assertEqual(data_directory(self.root / "third-copy", args), first)
            args.replay = "sample.pcap"
            self.assertEqual(data_directory(self.root / "new-unzip", args), self.root / "new-unzip" / "replay-data")
            args.data_dir = str(self.root / "explicit")
            self.assertEqual(data_directory(self.root, args), self.root / "explicit")

    def test_merge_old_and_new_copies_is_idempotent_and_preserves_originals(self):
        legacy = self.write(self.root / "old" / "history.json", history(record(), record("old-only", x=120000)))
        active = self.write(self.root / "new" / "history.json", history(record(), record("new-only", map_id=1010), language="ko", sound=False))
        before = [p.read_bytes() for p in (active, legacy)]
        destination = self.root / "profile"
        first = migrate_legacy(destination, [active, legacy])
        second = migrate_legacy(destination, [active, legacy])
        self.assertEqual(first, second)
        self.assertEqual({r["id"] for r in first["records"]}, {"bag-A", "old-only", "new-only"})
        self.assertEqual(first["language"], "ko")
        self.assertFalse(first["sound"])
        self.assertEqual([p.read_bytes() for p in (active, legacy)], before)
        self.assertEqual(len(list((destination / "backups").glob("imported-*.json"))), 2)

    def test_merge_keeps_first_open_but_uses_latest_update_for_timer(self):
        original = record()
        stale = record(status="unknown", opened_at=None, first_seen=original["first_seen"] + 900,
                       refresh_anchor_at=original["first_seen"] + 900, last_seen=original["last_seen"] + 1000)
        merged = merge_histories(history(original, language="ja"), [history(stale, language="en")])
        row = merged["records"][0]
        self.assertEqual(row["status"], "opened")
        for key in ("first_seen", "opened_at", "x", "y", "z"):
            self.assertEqual(row[key], original[key], key)
        self.assertEqual(row["refresh_anchor_at"], stale["last_seen"])
        self.assertEqual(merge_histories(merged, [history(original)])["records"][0]["refresh_anchor_at"], stale["last_seen"])
        self.assertEqual(merged["language"], "ja")

    def test_restart_preserves_all_maps_times_and_never_replays_old_alerts(self):
        rows = [record(), record("bag-B", map_id=1010, status="seen", opened_at=None)]
        self.write(self.root / "history.json", history(*rows, language="ja", sound=False))
        first = MapState(self.root, MAPS)
        first.flush()
        second = MapState(self.root, MAPS)
        snapshot = second.snapshot()
        self.assertEqual(snapshot["storage"]["record_count"], 2)
        self.assertEqual(snapshot["storage"]["map_counts"], {"1110": 1, "1010": 1})
        self.assertEqual(snapshot["alert_seq"], 0)
        self.assertIsNone(snapshot["latest_alert"])
        self.assertEqual(snapshot["language"], "ja")
        self.assertFalse(snapshot["sound"])
        for row in rows:
            restored = second.records[row["id"]]
            for key in ("first_seen", "opened_at", "x", "y", "z", "spawned_at"):
                self.assertEqual(restored[key], row[key], key)
            self.assertEqual(restored["refresh_anchor_at"], row["last_seen"])
        self.assertEqual(second.records["bag-A"]["status"], "opened")
        self.assertEqual(second.records["bag-B"]["status"], "unknown")

    def test_same_spawn_with_different_ids_keeps_confirmed_open_after_merge_and_reentry(self):
        opened = record()
        newer = record("other-install-id", status="seen", opened_at=None,
                       received_at=opened["first_seen"] + 500, last_seen=opened["last_seen"] + 500)
        self.write(self.root / "history.json", history(opened, newer))
        restored = MapState(self.root, MAPS)
        visible = [row for row in restored.records.values() if not row.get("superseded_by")]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["status"], "opened")
        self.assertEqual(visible[0]["opened_at"], opened["opened_at"])
        event = {"kind": "object", "at": opened["first_seen"] + 600, "connection_id": "test",
                 "entity_id": 43, "template_id": opened["template_id"], "x": opened["x"],
                 "y": opened["y"], "z": opened["z"], "spawned_at": opened["spawned_at"], "states": [1, 1, 6]}
        self.assertIsNone(restored.handle(event))
        visible = [row for row in restored.records.values() if not row.get("superseded_by")]
        self.assertEqual(visible[0]["status"], "opened")
        self.assertEqual(visible[0]["opened_at"], opened["opened_at"])

    def test_open_fact_does_not_transfer_to_unknown_or_different_spawn_cycle(self):
        for stamp in (0, record()["spawned_at"] + 3600000):
            with self.subTest(spawned_at=stamp):
                directory = self.root / str(stamp)
                self.write(directory / "history.json", history(record(), record("new-cycle", status="seen", opened_at=None,
                           spawned_at=stamp, first_seen=record()["first_seen"] + 3600,
                           refresh_anchor_at=record()["first_seen"] + 3600, received_at=record()["first_seen"] + 3600)))
                restored = MapState(directory, MAPS)
                self.assertEqual(restored.records["new-cycle"]["status"], "unknown")
                self.assertIsNone(restored.records["new-cycle"]["opened_at"])

    def test_truncated_main_recovers_backup_and_preserves_damaged_bytes(self):
        store = HistoryStore(self.root)
        store.save(history(record()))
        store.save(history(record(), record("bag-B", map_id=1010)))
        store.save(history(record(), record("bag-B", map_id=1010)))
        broken = b'{"schema": 1, "records": ['
        store.path.write_bytes(broken)
        restored = MapState(self.root, MAPS)
        self.assertEqual(set(restored.records), {"bag-A", "bag-B"})
        self.assertEqual(restored.store.recovered_from, str(store.previous))
        self.assertIsNone(restored.storage_error)
        preserved = list(store.backups.glob("unreadable-*.json"))
        self.assertTrue(any(p.read_bytes() == broken for p in preserved))
        self.assertEqual(len(json.loads(store.path.read_text("utf-8"))["records"]), 2)

    def test_unrecoverable_file_is_never_replaced_by_empty_history(self):
        path = self.root / "history.json"
        path.write_bytes(b"truncated and no backup")
        restored = MapState(self.root, MAPS)
        restored.flush()
        self.assertEqual(path.read_bytes(), b"truncated and no backup")
        self.assertTrue(restored.snapshot()["storage"]["error"])

    def test_recovery_includes_newer_hourly_snapshot_after_interrupted_main_write(self):
        self.write(self.root / "history.previous.json", history(record()))
        self.write(self.root / "backups" / "history-newer.json", history(record(), record("newer-bag", map_id=1010)))
        (self.root / "history.json").write_bytes(b"broken")
        restored = MapState(self.root, MAPS)
        self.assertEqual(set(restored.records), {"bag-A", "newer-bag"})

    def test_damaged_single_row_is_recovered_by_id_without_losing_healthy_rows(self):
        original = record()
        self.write(self.root / "history.previous.json", history(original))
        self.write(self.root / "history.json", history(record(x=None), record("healthy-bag", map_id=1010)))
        restored = MapState(self.root, MAPS)
        self.assertEqual(set(restored.records), {"bag-A", "healthy-bag"})
        self.assertEqual(restored.records["bag-A"]["x"], original["x"])
        self.assertEqual(restored.records["bag-A"]["opened_at"], original["opened_at"])
        self.assertIsNone(restored.storage_error)
        self.assertIsNotNone(restored.store.recovered_from)

    def test_one_invalid_row_does_not_hide_other_records_and_original_is_preserved(self):
        path = self.write(self.root / "history.json", history(record(), {"id": "bad", "status": {}}, None))
        original = path.read_bytes()
        restored = MapState(self.root, MAPS)
        self.assertEqual(set(restored.records), {"bag-A"})
        self.assertTrue(any(p.read_bytes() == original for p in (self.root / "backups").glob("unreadable-records-*.json")))

    def test_failed_atomic_replace_leaves_current_file_unchanged_and_removes_temp(self):
        path = self.write(self.root / "history.json", history(record()))
        original = path.read_bytes()
        with patch("aion2_redbag_map.history_store.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                atomic_write(path, b"new content")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_readonly_viewer_follows_writer_without_overwriting_data_or_preferences(self):
        writer = MapState(self.root, MAPS)
        writer.records["bag-A"] = record()
        writer.flush()
        viewer = MapState(self.root, MAPS, mode="offline", readonly=True)
        viewer.select_map(1010)
        viewer.set_language("ko")
        writer.records["bag-B"] = record("bag-B", map_id=1010)
        writer.flush()
        before = writer.path.read_bytes()
        viewer.flush()
        snapshot = viewer.snapshot()
        self.assertEqual(snapshot["storage"]["record_count"], 2)
        self.assertTrue(snapshot["storage"]["readonly"])
        self.assertEqual(snapshot["selected_map_id"], 1010)
        self.assertEqual(snapshot["language"], "ko")
        self.assertEqual(writer.path.read_bytes(), before)
        self.assertEqual(json.loads(before)["language"], "zh-CN")

    def test_exclusive_writer_lock_is_released_after_context_exit(self):
        with WriterLock(self.root):
            with self.assertRaises(HistoryInUse):
                with WriterLock(self.root):
                    self.fail("second writer obtained archive")
        with WriterLock(self.root):
            pass

    def test_storage_keeps_old_cycles_beyond_previous_2000_record_limit(self):
        state = MapState(self.root, MAPS)
        state.records = {f"bag-{i}": record(f"bag-{i}", x=i * 1000, superseded_by="latest") for i in range(2001)}
        state.flush()
        saved = json.loads(state.path.read_text("utf-8"))
        self.assertEqual(len(saved["records"]), 2001)
        self.assertEqual(state.snapshot()["storage"]["record_count"], 2001)


if __name__ == "__main__":
    unittest.main()
