import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.model import MapState

MAPS = [{"id": 1110, "name": "阿尔特盖德"}, {"id": 1010, "name": "Verteron"}]


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = MapState(Path(self.tmp.name), MAPS)
        self.at = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, kind="object", **extra):
        data = {"kind": kind, "at": self.at, "connection_id": "flow-A", "entity_id": 42895,
                "template_id": 1301199, "x": 106518.438, "y": 71755.414, "z": 4347,
                "states": [1, 1, 6], "spawned_at": 1792749938682}
        data.update(extra)
        return data

    def row(self):
        return self.state.snapshot()["records"][0]

    def test_observation_interaction_then_confirmed_open(self):
        alert = self.state.handle(self.event())
        self.assertEqual(alert["seq"], 1)
        self.assertEqual(self.row()["status"], "seen")
        self.assertIsNone(self.state.handle(self.event("use", at=self.at+9)))
        self.assertEqual(self.row()["status"], "used")
        self.state.handle(self.event("state", a=4, b=3, at=self.at+14))
        self.assertEqual(self.row()["status"], "opened")
        self.state.handle(self.event("gone", at=self.at+15))
        self.assertEqual(self.row()["status"], "opened")

    def test_retransmission_reentry_and_restart_do_not_notify_again(self):
        self.state.handle(self.event())
        self.assertIsNone(self.state.handle(self.event()))
        self.assertEqual(len(self.state.records), 1)
        self.state.handle(self.event("gone"))
        self.assertEqual(self.row()["status"], "unknown")
        self.assertIsNone(self.state.handle(self.event(at=self.at+1)))
        self.assertEqual(self.row()["status"], "seen")
        restored = MapState(Path(self.tmp.name), MAPS)
        self.assertEqual(restored.snapshot()["records"][0]["status"], "unknown")
        self.assertEqual(restored.alert_seq, 0)
        self.assertIsNone(restored.handle(self.event(at=self.at+2)))

    def test_same_location_new_spawn_not_suppressed(self):
        self.state.handle(self.event())
        self.state.handle(self.event("state", a=4, b=3))
        new = self.state.handle(self.event(at=self.at+30, spawned_at=1792749968682))
        self.assertEqual(new["seq"], 2)
        self.assertEqual(self.row()["status"], "seen")

    def test_connection_and_same_map_reload_invalidate_scopes(self):
        self.state.handle(self.event("map", map_id=1110, reset=True))
        self.state.handle(self.event())
        self.state.handle(self.event("map", map_id=1110, reset=True))
        self.assertEqual(self.row()["status"], "unknown")
        self.state.handle(self.event("state", a=4, b=3))
        self.assertEqual(self.row()["status"], "unknown")
        self.state.handle(self.event(entity_id=99, spawned_at=1792749939999, x=120000, at=self.at+1))
        self.state.handle(self.event("disconnect"))
        self.assertTrue(all(r["status"] == "unknown" for r in self.state.records.values()))
        self.assertEqual(self.state.map_source, "manual")

    def test_browse_map_does_not_relabel_live_objects_after_detection(self):
        self.state.handle(self.event("map", map_id=1110))
        self.state.handle(self.event("map", map_id=1010, map_source="world_map_response"))
        self.assertEqual(self.state.current_map_id, 1110)
        self.state.select_map(1010)
        self.state.handle(self.event())
        self.assertEqual(self.row()["map_id"], 1110)
        self.assertEqual(self.state.selected_map_id, 1010)

    def test_pause_and_stale_stream_cannot_keep_active_points(self):
        self.state.handle(self.event())
        self.state.set_paused(True)
        self.assertEqual(self.row()["status"], "unknown")
        self.assertIsNone(self.state.handle(self.event(spawned_at=999999999)))
        self.state.set_paused(False)
        self.state.handle(self.event(spawned_at=1792749948888, at=self.at+1))
        self.state.capture_status({"phase": "observing", "last_frame_at": self.at-25})
        self.assertEqual(self.row()["status"], "unknown")

    def test_nonred_bad_coordinates_and_initial_depleted_do_not_alert(self):
        self.assertIsNone(self.state.handle(self.event(template_id=1300081)))
        self.assertIsNone(self.state.handle(self.event(x=float('nan'))))
        self.assertEqual(len(self.state.records), 0)
        self.assertIsNone(self.state.handle(self.event(states=[4, 3, 6])))
        self.assertEqual(self.row()["status"], "opened")

    def test_historical_seed_is_not_live(self):
        seed = json.loads((Path(__file__).resolve().parent / "fixtures/synthetic-seed.json").read_text("utf-8"))
        seeded = MapState(Path(self.tmp.name), MAPS, seed)
        self.assertEqual(seeded.alert_seq, 0)
        self.assertEqual(seeded.snapshot()["records"][0]["status"], "opened")
        self.assertEqual(seeded.snapshot()["records"][0]["source"], "history")

    def test_refresh_estimate_repeats_hourly_and_expires_five_minutes_after_each_target(self):
        self.state.handle(self.event())
        self.state.handle(self.event("state", a=4, b=3, at=self.at+14))
        latest = self.at + 14
        for elapsed, display, cycle in ((0, "opened", 1), (299, "opened", 1),
                                       (3299.999, "opened", 1), (3300, "soon", 1),
                                       (3600, "soon", 1), (3899.999, "soon", 1),
                                       (3900, "opened", 2), (6899.999, "opened", 2),
                                       (6900, "soon", 2), (7200, "soon", 2),
                                       (7499.999, "soon", 2), (7500, "opened", 3),
                                       (10500, "soon", 3), (11100, "opened", 4),
                                       (86400, "soon", 24), (86700, "opened", 25)):
            with self.subTest(elapsed=elapsed), patch("aion2_redbag_map.model.time.time", return_value=latest+elapsed):
                snapshot = self.state.snapshot()
                row = snapshot["records"][0]
                self.assertEqual(row["display_status"], display)
                self.assertEqual(row["status"], "opened")
                self.assertEqual(row["opened_at"], self.at+14)
                self.assertEqual(row["refresh_anchor_at"], latest)
                self.assertEqual(row["first_seen"], self.at)
                self.assertEqual(row["refresh_cycle"], cycle)
                self.assertEqual(row["refresh_soon_at"], latest+cycle*3600-300)
                self.assertEqual(row["refresh_expected_at"], latest+cycle*3600)
                self.assertEqual(row["refresh_window_end_at"], latest+cycle*3600+300)
                self.assertTrue(row["refresh_estimated"])
                self.assertEqual(snapshot["alert_seq"], 1)

    def test_expired_refresh_window_restores_the_actual_observed_state(self):
        self.state.handle(self.event())
        for kind, expected in ((None, "seen"), ("use", "used"), ("gone", "unknown")):
            with self.subTest(expected=expected):
                if kind:
                    self.state.handle(self.event(kind, at=self.at+20))
                anchor = self.at + 20 if kind else self.at
                with patch("aion2_redbag_map.model.time.time", return_value=anchor+3900):
                    self.assertEqual(self.row()["display_status"], expected)
                with patch("aion2_redbag_map.model.time.time", return_value=anchor+6900):
                    self.assertEqual(self.row()["display_status"], "soon")
                self.assertEqual(self.row()["status"], expected)
        self.assertEqual(self.state.alert_seq, 1)

    def test_refresh_anchor_moves_to_latest_reentry_and_survives_restart(self):
        self.state.handle(self.event())
        self.state.handle(self.event(at=self.at+900))
        self.state.handle(self.event("gone", at=self.at+901))
        self.assertIsNone(self.state.handle(self.event(at=self.at+2000)))
        restored = MapState(Path(self.tmp.name), MAPS)
        self.assertIsNone(restored.handle(self.event(at=self.at+3000)))
        with patch("aion2_redbag_map.model.time.time", return_value=self.at+6300):
            current = [r for r in restored.snapshot()["records"] if not r.get("superseded_by")]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["first_seen"], self.at)
        self.assertEqual(current[0]["refresh_anchor_at"], self.at+3000)
        self.assertEqual(current[0]["display_status"], "soon")
        self.assertEqual(restored.alert_seq, 0)

    def test_new_cycle_restarts_estimate_and_late_old_cycle_cannot_replace_it(self):
        original = self.state.handle(self.event())["record_id"]
        self.state.handle(self.event("state", a=4, b=3, at=self.at+14))
        next_spawn = 1792749938682 + 3600000
        new = self.state.handle(self.event(at=self.at+3600, entity_id=54321, spawned_at=next_spawn))
        self.assertEqual(new["seq"], 2)
        self.assertEqual(self.state.records[original]["superseded_by"], new["record_id"])
        self.assertIsNone(self.state.handle(self.event(at=self.at+3602, entity_id=12345)))
        with patch("aion2_redbag_map.model.time.time", return_value=self.at+3603):
            current = [r for r in self.state.snapshot()["records"] if not r.get("superseded_by")]
        self.assertEqual([r["id"] for r in current], [new["record_id"]])
        self.assertEqual(current[0]["display_status"], "seen")
        self.assertEqual(current[0]["refresh_anchor_at"], self.at+3600)
        self.assertEqual(self.state.alert_seq, 2)

    def test_refresh_locations_are_separate_by_map_and_elevation(self):
        self.state.handle(self.event())
        self.state.handle(self.event(entity_id=54321, z=5347, at=self.at+1))
        self.state.handle(self.event("map", connection_id="flow-B", map_id=1010))
        self.state.handle(self.event(connection_id="flow-B", at=self.at+2))
        current = [r for r in self.state.snapshot()["records"] if not r.get("superseded_by")]
        self.assertEqual(len(current), 3)
        self.assertEqual({r["refresh_anchor_at"] for r in current}, {self.at, self.at+1, self.at+2})

    def test_legacy_history_keeps_first_discovery_but_times_from_latest_update(self):
        self.state.handle(self.event())
        self.state.handle(self.event("gone", at=self.at+1))
        self.state.handle(self.event(at=self.at+200))
        data = json.loads(self.state.path.read_text("utf-8"))
        for row in data["records"]:
            row.pop("refresh_anchor_at", None)
            row.pop("superseded_by", None)
            row["first_seen"] = row["received_at"]
        self.state.path.write_text(json.dumps(data), "utf-8")
        restored = MapState(Path(self.tmp.name), MAPS)
        with patch("aion2_redbag_map.model.time.time", return_value=self.at+3300):
            rows = restored.snapshot()["records"]
        self.assertEqual(sum(not r.get("superseded_by") for r in rows), 1)
        self.assertTrue(all(r["refresh_anchor_at"] == self.at+200 for r in rows))
        self.assertTrue(all(r["first_seen"] == self.at for r in rows))
        for value in (None, 0, -1, True):
            with self.subTest(value=value):
                record = next(iter(restored.records.values()))
                for field in ("first_seen", "refresh_anchor_at", "last_seen", "opened_at", "received_at"):
                    record[field] = value
                row = restored._display_record(record, self.at+7200)
                self.assertIsNone(row["refresh_soon_at"])
                self.assertIsNone(row["refresh_expected_at"])
                self.assertIsNone(row["refresh_window_end_at"])
                self.assertIsNone(row["refresh_cycle"])
                self.assertFalse(row["refresh_estimated"])
                self.assertEqual(row["display_status"], row["status"])

    def test_update_moves_window_without_new_alert_or_rewriting_first_open_time(self):
        self.state.handle(self.event())
        self.state.handle(self.event("state", a=4, b=3, at=self.at+10))
        with patch("aion2_redbag_map.model.time.time", return_value=self.at+3310):
            self.assertEqual(self.row()["display_status"], "soon")
        self.assertIsNone(self.state.handle(self.event(at=self.at+1800)))
        self.assertEqual(self.row()["refresh_anchor_at"], self.at+1800)
        with patch("aion2_redbag_map.model.time.time", return_value=self.at+3310):
            self.assertEqual(self.row()["display_status"], "opened")
        self.assertEqual(self.row()["opened_at"], self.at+10)
        self.assertEqual(self.row()["first_seen"], self.at)
        self.assertEqual(self.state.alert_seq, 1)
        self.state.handle(self.event("state", a=4, b=3, at=self.at+2000))
        self.assertEqual(self.row()["refresh_anchor_at"], self.at+2000)
        self.assertEqual(self.row()["opened_at"], self.at+10)
        self.assertEqual(self.state.alert_seq, 1)

    def test_delayed_events_and_preferences_do_not_move_timer_backwards_or_to_now(self):
        self.state.handle(self.event())
        self.state.handle(self.event(at=self.at+900))
        self.state.handle(self.event(at=self.at+60))
        self.assertEqual(self.row()["last_seen"], self.at+900)
        self.assertEqual(self.row()["refresh_anchor_at"], self.at+900)
        self.state.set_language("en")
        self.state.set_sound(False)
        self.state.select_map(1010)
        self.state.capture_status({"phase": "paused"})
        self.assertEqual(self.row()["refresh_anchor_at"], self.at+900)
        self.assertEqual(self.row()["first_seen"], self.at)
        restored = MapState(Path(self.tmp.name), MAPS)
        self.assertEqual(next(iter(restored.records.values()))["refresh_anchor_at"], self.at+900)
        self.assertEqual(restored.alert_seq, 0)

    def test_open_on_older_id_immediately_updates_visible_same_spawn(self):
        first_id = self.state.handle(self.event())["record_id"]
        self.assertIsNone(self.state.handle(self.event(entity_id=54321, at=self.at+100)))
        visible = [row for row in self.state.records.values() if not row.get("superseded_by")][0]
        self.assertNotEqual(visible["id"], first_id)
        self.assertIsNone(self.state.handle(self.event("state", a=4, b=3, at=self.at+120)))
        self.assertEqual(visible["status"], "opened")
        self.assertEqual(visible["opened_at"], self.at+120)
        self.assertEqual(visible["refresh_anchor_at"], self.at+120)
        self.assertEqual(visible["first_seen"], self.at)
        self.state.handle(self.event("state", entity_id=54321, a=4, b=3, at=self.at+160))
        self.state.handle(self.event("use", at=self.at+150))
        self.assertEqual(visible["opened_at"], self.at+120, "duplicate events retain the first open time")
        self.assertEqual(visible["refresh_anchor_at"], self.at+160, "late old-ID updates cannot move the shared clock back")
        self.assertTrue(all(row["status"] == "opened" for row in self.state.records.values()))
        self.assertEqual(self.state.alert_seq, 1)
        restored = MapState(Path(self.tmp.name), MAPS)
        latest = [row for row in restored.records.values() if not row.get("superseded_by")][0]
        self.assertEqual(latest["opened_at"], self.at+120)
        self.assertEqual(latest["refresh_anchor_at"], self.at+160)

    def test_touch_open_propagation_stays_within_known_spawn_location(self):
        variants = {
            "different_spawn": {"spawned_at": 1792749938683},
            "unknown_spawn": {"spawned_at": 0},
            "other_floor": {"z": 5347},
            "other_template": {"template_id": 1301200},
            "other_map": {"connection_id": "flow-B"},
        }
        for label, changes in variants.items():
            with self.subTest(label=label):
                state = MapState(Path(self.tmp.name) / label, MAPS)
                state.handle(self.event())
                if label == "other_map":
                    state.handle(self.event("map", connection_id="flow-B", map_id=1010))
                state.handle(self.event(entity_id=54321, at=self.at+1000, **changes))
                other = next(row for row in state.records.values() if row["entity_id"] == 54321)
                state.handle(self.event("state", a=4, b=3, at=self.at+1100))
                self.assertEqual(other["status"], "seen")
                self.assertIsNone(other["opened_at"])
                self.assertEqual(other["refresh_anchor_at"], self.at+1000)
        unknown = MapState(Path(self.tmp.name) / "both-unknown", MAPS)
        unknown.handle(self.event(spawned_at=0))
        unknown.handle(self.event(entity_id=54321, at=self.at+1000, spawned_at=0))
        other = next(row for row in unknown.records.values() if row["entity_id"] == 54321)
        unknown.handle(self.event("state", a=4, b=3, at=self.at+1100))
        self.assertEqual(other["status"], "seen", "unknown stamps do not establish a shared spawn")
        self.assertIsNone(other["opened_at"])

    def test_repeated_use_and_opened_gone_advance_time_without_changing_open_fact(self):
        self.state.handle(self.event())
        self.state.handle(self.event("use", at=self.at+10))
        self.state.handle(self.event("use", at=self.at+20))
        self.assertEqual(self.row()["status"], "used")
        self.assertEqual(self.row()["refresh_anchor_at"], self.at+20)
        self.assertEqual(sum(event["kind"] == "used" for event in self.state.events), 1)
        self.state.handle(self.event("state", a=4, b=3, at=self.at+30))
        self.state.handle(self.event("use", at=self.at+50))
        self.state.handle(self.event("use", at=self.at+40))
        self.assertEqual(self.row()["last_seen"], self.at+50)
        self.state.handle(self.event("gone", at=self.at+60))
        self.assertEqual(self.row()["status"], "opened")
        self.assertEqual(self.row()["opened_at"], self.at+30)
        self.assertEqual(self.row()["first_seen"], self.at)
        self.assertEqual(self.row()["last_seen"], self.at+60)
        self.assertEqual(self.row()["refresh_anchor_at"], self.at+60)
        self.assertEqual(self.state.bindings, {})
        self.assertEqual(self.state.alert_seq, 1)
        restored = MapState(Path(self.tmp.name), MAPS)
        row = next(iter(restored.records.values()))
        self.assertEqual(row["opened_at"], self.at+30)
        self.assertEqual(row["refresh_anchor_at"], self.at+60)

    def test_delayed_gone_after_open_cannot_move_latest_time_backwards(self):
        self.state.handle(self.event())
        self.state.handle(self.event("state", a=4, b=3, at=self.at+10))
        self.state.handle(self.event(at=self.at+90))
        self.state.handle(self.event("gone", at=self.at+20))
        self.assertEqual(self.row()["status"], "opened")
        self.assertEqual(self.row()["opened_at"], self.at+10)
        self.assertEqual(self.row()["last_seen"], self.at+90)
        self.assertEqual(self.row()["refresh_anchor_at"], self.at+90)
        self.assertEqual(self.state.alert_seq, 1)


if __name__ == "__main__":
    unittest.main()
