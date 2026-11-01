import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.model import MapState

MAPS = [{"id": 1110, "name": "阿尔特盖德"}, {"id": 1010, "name": "Verteron"}]
AT = 1792764000.0


class PlayerPositionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = patch("aion2_redbag_map.model.time.time", return_value=AT)
        self.clock.start()
        self.state = MapState(Path(self.tmp.name), MAPS)
        self.state.capture_status({"phase": "observing", "last_frame_at": AT})

    def tearDown(self):
        self.clock.stop()
        self.tmp.cleanup()

    def enter(self, **extra):
        self.state.handle(dict(kind="map", connection_id="flow-A", at=AT, map_id=1110, **extra))

    def position(self, **extra):
        event = dict(kind="player_position", connection_id="flow-A", at=AT + 1,
                     x=1000.5, y=2000.25, z=300.0, source="self_status",
                     source_opcode="0x382a", identity_source="stats_consensus")
        event.update(extra)
        return self.state.handle(event)

    def snapshot(self, at):
        with patch("aion2_redbag_map.model.time.time", return_value=at):
            return self.state.snapshot()

    def test_requires_actual_map_and_matching_connection(self):
        self.position()
        self.assertIsNone(self.state.snapshot()["player"])
        self.enter(map_source="world_map_response")
        self.position()
        self.assertIsNone(self.state.snapshot()["player"])
        self.enter()
        self.position(connection_id="other-flow")
        self.assertIsNone(self.state.snapshot()["player"])
        self.position()
        self.assertEqual(self.state.snapshot()["player"]["map_id"], 1110)
        self.state.select_map(1010)
        self.assertEqual(self.state.snapshot()["player"]["map_id"], 1110,
                         "browsing another map cannot relabel the player")

    def test_schema_exact_expiry_and_no_history_or_alert_side_effects(self):
        self.enter()
        before = self.state.path.read_bytes()
        events = list(self.state.events)
        with patch.object(self.state, "_save") as save:
            self.assertIsNone(self.position())
            save.assert_not_called()
        player = self.snapshot(AT + 10.999)["player"]
        self.assertEqual(set(player), {"x", "y", "z", "map_id", "observed_at", "expires_at",
                                       "source", "identity_source", "stale"})
        self.assertFalse(player["stale"])
        self.assertEqual(player["expires_at"], AT + 11)
        self.assertTrue(self.snapshot(AT + 11)["player"]["stale"])
        self.assertEqual(self.state.alert_seq, 0)
        self.assertIsNone(self.state.latest_alert)
        self.assertEqual(self.state.events, events)
        self.assertEqual(self.state.path.read_bytes(), before)
        self.assertNotIn("player", self.state.export_history())
        self.assertNotIn("player", json.loads(before))
        restored = MapState(Path(self.tmp.name), MAPS)
        self.assertIsNone(restored.snapshot()["player"])

    def test_ordered_equal_timestamps_allowed_but_late_positions_cannot_regress(self):
        self.enter()
        self.position(at=AT + 3)
        self.position(at=AT + 2, x=9000)
        self.assertEqual(self.state.player["x"], 1000.5)
        self.position(at=AT + 3, x=3000)
        self.assertEqual(self.state.player["x"], 3000)
        self.state.handle(dict(kind="player_position_reset", connection_id="flow-A",
                               at=AT + 4, reason="identity_changed"))
        self.assertIsNone(self.state.snapshot()["player"])
        self.position(at=AT + 3, x=9000)
        self.assertIsNone(self.state.snapshot()["player"])
        self.position(at=AT + 4, x=4000)
        self.assertEqual(self.state.player["x"], 4000)

    def test_reset_only_invalidates_matching_source(self):
        self.enter()
        self.position()
        self.state.handle(dict(kind="player_position_reset", connection_id="flow-B", at=AT + 2))
        self.assertIsNotNone(self.state.snapshot()["player"])
        self.state.handle(dict(kind="disconnect", connection_id="flow-B", at=AT + 2))
        self.assertIsNotNone(self.state.snapshot()["player"])

    def test_disconnect_gap_stream_and_map_reset_require_new_map(self):
        for index, kind in enumerate(("disconnect", "gap", "stream_reset", "map_reset")):
            with self.subTest(kind=kind):
                base = AT + index * 10
                self.state.handle(dict(kind="map", connection_id="flow-A", at=base, map_id=1110))
                self.position(at=base + 1)
                self.assertIsNotNone(self.state.snapshot()["player"])
                self.state.handle(dict(kind=kind, connection_id="flow-A", at=base + 2))
                self.assertIsNone(self.state.snapshot()["player"])
                self.position(at=base + 3)
                self.assertIsNone(self.state.snapshot()["player"])
                self.state.handle(dict(kind="map", connection_id="flow-A", at=base + 4,
                                       map_id=1110, reset=True))
                self.position(at=base + 3)
                self.assertIsNone(self.state.snapshot()["player"])
                self.position(at=base + 5)
                self.assertIsNotNone(self.state.snapshot()["player"])

    def test_pause_loss_and_new_map_invalidate_immediately(self):
        self.enter()
        self.position()
        self.state.set_paused(True)
        self.assertIsNone(self.state.snapshot()["player"])
        self.state.handle(dict(kind="map", connection_id="flow-A", at=AT + 1, map_id=1110))
        self.assertIsNone(self.state.map_connection, "queued map data while paused cannot restore its scope")
        self.state.set_paused(False)
        self.position(at=AT + 2)
        self.assertIsNone(self.state.snapshot()["player"])
        self.state.capture_status({"phase": "observing"})
        self.enter()
        self.position(at=AT + 2)
        self.assertIsNotNone(self.state.snapshot()["player"])
        self.state.capture_status({"phase": "waiting"})
        self.assertIsNone(self.state.snapshot()["player"])
        self.position(at=AT + 3)
        self.assertIsNone(self.state.snapshot()["player"])
        self.state.capture_status({"phase": "observing"})
        self.position(at=AT + 3)
        self.state.handle(dict(kind="map", connection_id="flow-A", at=AT + 4, map_id=1010))
        self.assertIsNone(self.state.snapshot()["player"])
        self.position(at=AT + 3)
        self.assertIsNone(self.state.snapshot()["player"])
        self.position(at=AT + 5)
        self.assertEqual(self.state.player["map_id"], 1010)
        self.state.handle(dict(kind="map", connection_id="flow-A", at=AT + 2, map_id=1110))
        self.assertEqual(self.state.current_map_id, 1010)

    def test_same_map_scene_reset_and_stale_stream_clear_position(self):
        self.enter()
        self.position()
        self.state.handle(dict(kind="map", connection_id="flow-A", at=AT + 2, map_id=1110, reset=True))
        self.assertIsNone(self.state.snapshot()["player"])
        self.position(at=AT + 3)
        self.assertIsNotNone(self.state.snapshot()["player"])
        self.assertIsNone(self.snapshot(AT + 21)["player"])

    def test_invalid_coordinates_sources_and_replay_never_get_a_player(self):
        self.enter()
        for extra in ({"x": float("nan")}, {"y": float("inf")}, {"z": True},
                      {"x": "100"}, {"z": 1_000_001}, {"at": 0},
                      {"source": "object_approximation"}, {"identity_source": "guess"}):
            with self.subTest(extra=extra):
                self.position(**extra)
                self.assertIsNone(self.state.snapshot()["player"])
        self.state.mode = "replay"
        self.position()
        self.assertIsNone(self.state.snapshot()["player"])

    def test_position_does_not_consume_or_replace_real_bag_alert(self):
        self.enter()
        alert = self.state.handle(dict(kind="object", connection_id="flow-A", at=AT,
                                       entity_id=42, template_id=1301199, x=5000, y=6000, z=700,
                                       spawned_at=1792764000000, states=[1, 1, 6]))
        before = self.state.path.read_bytes()
        self.assertEqual(alert["seq"], 1)
        self.position()
        self.position(at=AT + 2, x=1100)
        snapshot = self.state.snapshot()
        self.assertEqual(snapshot["latest_alert"], alert)
        self.assertEqual(snapshot["alert_seq"], 1)
        self.assertEqual(len(snapshot["records"]), 1)
        self.assertEqual(self.state.path.read_bytes(), before)
        self.assertNotIn("player", self.state.export_history())


if __name__ == "__main__":
    unittest.main()
