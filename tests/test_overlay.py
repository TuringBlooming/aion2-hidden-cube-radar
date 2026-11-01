import copy
from collections import OrderedDict
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.overlay import (DesktopOverlay, NoRedirect, OverlayLock, OverlayState, approximate_distance,
                     bag_focus_scale, target_indicator, display_status, latest_time, load_settings, local_clock, main,
                     player_reference, queue_latest_event, render_map_frame, TILE_CACHE_LIMIT,
                     visible_records, visible_tiles)
from aion2_redbag_map.overlay_text import TEXT, text


def record(identity="A", **changes):
    row = {"id": identity, "map_id": 1110, "x": 106518.438, "y": 71755.414,
           "status": "opened", "first_seen": 10000, "received_at": 10000,
           "last_seen": 10020, "opened_at": 10014, "refresh_anchor_at": 10000}
    row.update(changes)
    return row


def snapshot(rows=None, **changes):
    return {"app": "AION2 RedBag Map", "session_id": "session-A", "alert_seq": 0,
            "mode": "live", "current_map_id": 1110, "selected_map_id": 1110,
            "records": [record()] if rows is None else rows, **changes}


def player(**changes):
    return {"x": 106218.438, "y": 71355.414, "z": 1200, "map_id": 1110,
            "observed_at": 12000, "expires_at": 12010, "source": "self_status",
            "identity_source": "stats_consensus", "stale": False, **changes}


def positioned(**changes):
    return snapshot(capture={"phase": "observing"}, player=player(), **changes)


def headless_view():
    """Exercise controller math without Tk, native windows, or a local service."""
    view = DesktopOverlay.__new__(DesktopOverlay)
    view.state = OverlayState()
    view.maps = [{"id": 1110, "x": 0, "y": 0, "w": 200000, "h": 200000},
                 {"id": 1010, "x": 0, "y": 0, "w": 100000, "h": 100000}]
    view.width, view.height = 360, 300
    view.cx = view.cy = 100000
    view.fit_scale = .001455
    view.scale = view.fit_scale * 4
    view.zoom_ratio = 4.0
    view.render_key = None
    view.raster_mode = "overview"
    view.collapsed = False
    view.tile_photos = OrderedDict()
    view.paint = Mock()
    view.save_settings = Mock()
    return view


class OverlayModelTests(unittest.TestCase):
    def test_latest_time_drives_each_hour_window_without_mutating_original(self):
        row = record()
        original = copy.deepcopy(row)
        self.assertEqual(latest_time(row), 10020)
        for seconds, expected in [(3299, "opened"), (3300, "soon"), (3600, "soon"),
                                  (3899, "soon"), (3900, "opened"), (6900, "soon"), (7500, "opened")]:
            self.assertEqual(display_status(row, 10020 + seconds), expected)
        self.assertEqual(row, original)
        row["last_seen"] = 12000
        self.assertEqual(latest_time(row), 12000)
        self.assertEqual(display_status(row, 10020 + 3600), "opened")
        self.assertEqual(display_status(row, 12000 + 3600), "soon")

    def test_invalid_timestamps_and_superseded_cycles_are_not_estimated(self):
        self.assertIsNone(latest_time({"first_seen": True, "last_seen": float("nan"), "opened_at": -1}))
        self.assertEqual(display_status(record(superseded_by="B"), 10020 + 3600), "opened")
        self.assertEqual(display_status({"status": "unexpected"}, 99999), "unknown")

    def test_loading_current_session_or_restarting_never_replays_old_alert(self):
        model = OverlayState()
        initial = snapshot(alert_seq=5, latest_alert={"record_id": "A"})
        original = copy.deepcopy(initial)
        self.assertIsNone(model.accept(initial, 1000)["alert"])
        self.assertEqual(model.seq, 5)
        self.assertIsNone(model.accept(initial, 1001)["alert"])
        self.assertIsNone(model.accept(snapshot(session_id="new", alert_seq=1,
                                             latest_alert={"record_id": "A"}), 1002)["alert"])
        self.assertEqual(initial, original)

    def test_new_alert_selects_matching_map_and_open_status_updates_without_realert(self):
        model = OverlayState(follow=False)
        model.accept(snapshot())
        new = record("B", map_id=1010, status="seen", last_seen=12000, opened_at=None)
        result = model.accept(snapshot([record(), new], alert_seq=1, latest_alert={"record_id": "B"}), 12000)
        self.assertEqual(result["alert"], new)
        self.assertEqual(model.map_id, 1010)
        self.assertEqual(model.selected, "B")
        self.assertEqual(model.alert_until, 12020)
        opened = {**new, "status": "opened", "opened_at": 12010, "last_seen": 12010}
        update = model.accept(snapshot([record(), opened], alert_seq=1, latest_alert={"record_id": "B"}), 12010)
        self.assertIsNone(update["alert"])
        self.assertEqual(visible_records(model.snapshot, 1010)[0]["status"], "opened")

    def test_offline_history_does_not_emit_alert_and_disconnect_retains_data(self):
        model = OverlayState()
        model.accept(snapshot(mode="offline"))
        self.assertIsNone(model.accept(snapshot(mode="offline", alert_seq=1, latest_alert={"record_id": "A"}))["alert"])
        previous = model.snapshot
        model.disconnect()
        self.assertFalse(model.connected)
        self.assertIs(model.snapshot, previous)
        self.assertEqual(len(visible_records(model.snapshot, 1110)), 1)

    def test_visible_records_filter_old_cycles_and_invalid_locations(self):
        data = snapshot([record(), record("B", map_id=1010), record("C", superseded_by="A"),
                         record("D", x=float("nan")), None])
        self.assertEqual([row["id"] for row in visible_records(data, 1110)], ["A"])

    def test_map_change_follows_server_location_and_does_not_follow_browsed_map(self):
        model = OverlayState()
        model.accept(snapshot())
        result = model.accept(snapshot(selected_map_id=1010))
        self.assertFalse(result["fit"])
        self.assertEqual(model.map_id, 1110)
        result = model.accept(snapshot(current_map_id=1010, selected_map_id=1110))
        self.assertTrue(result["fit"])
        self.assertEqual(model.map_id, 1010)

    def test_only_expected_local_app_accepted_and_redirects_rejected(self):
        with self.assertRaises(ValueError):
            OverlayState().accept({"app": "something else"})
        with self.assertRaises(ValueError):
            NoRedirect().redirect_request(None, None, None, None, None)

    def test_settings_validation_and_single_instance_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            path.write_text(json.dumps({"x": 150, "y": 100, "alpha": 5, "collapsed": True, "layer": "true"}), encoding="utf-8")
            self.assertEqual(load_settings(path), {"x": 150, "y": 100, "alpha": 1.0, "collapsed": True,
                                                  "layer": False, "follow": True, "follow_centering_version": 0})
            path.write_text(json.dumps({"alpha": .75}), encoding="utf-8")
            self.assertEqual(load_settings(path)["alpha"], .75)
            path.write_text(json.dumps({"follow": False}), encoding="utf-8")
            self.assertTrue(load_settings(path)["follow"])
            path.write_text(json.dumps({"follow": False, "follow_centering_version": 1}), encoding="utf-8")
            self.assertFalse(load_settings(path)["follow"])
            path.write_text("invalid", encoding="utf-8")
            self.assertEqual(load_settings(path), {})
            with OverlayLock(root / "aion2_redbag_map.overlay.lock"):
                with self.assertRaises(OSError):
                    with OverlayLock(root / "aion2_redbag_map.overlay.lock"):
                        pass
            with OverlayLock(root / "aion2_redbag_map.overlay.lock"):
                pass

    def test_all_overlay_text_is_localized_and_clocks_keep_utc8(self):
        for key, translations in TEXT.items():
            self.assertEqual(len(translations), 4, key)
            self.assertTrue(all(isinstance(value, str) and value for value in translations), key)
            for language in ("zh-CN", "en", "ja", "ko"):
                self.assertTrue(text(key, language, count=3, seconds=4, meters=5))
        self.assertEqual(local_clock(1), "08:00:01")

    def test_reference_expires_at_ten_seconds_without_extrapolation(self):
        data = positioned()
        before = copy.deepcopy(data)
        fresh = player_reference(data, True, 12009.999)
        self.assertTrue(fresh["fresh"])
        self.assertEqual(fresh["age_seconds"], 9)
        self.assertEqual((fresh["x"], fresh["y"]), (player()["x"], player()["y"]))
        self.assertFalse(player_reference(data, True, 12010)["fresh"])
        self.assertFalse(player_reference(data, True, 11999)["fresh"])
        long_expiry = {**data, "player": player(expires_at=12100)}
        self.assertFalse(player_reference(long_expiry, True, 12010)["fresh"])
        self.assertEqual(data, before)

    def test_no_fresh_reference_after_pause_disconnect_or_invalid_identity(self):
        data = positioned()
        for changes in ({"paused": True}, {"mode": "offline"}, {"capture": {"phase": "lost"}},
                        {"current_map_id": 1010}, {"player": player(stale=True)}):
            with self.subTest(changes=changes):
                self.assertFalse(player_reference({**data, **changes}, True, 12001)["fresh"])
        self.assertFalse(player_reference(data, False, 12001)["fresh"])
        for changes in ({"x": float("nan")}, {"map_id": True}, {"source": "unknown"},
                        {"identity_source": "other"}, {"observed_at": 0}):
            with self.subTest(changes=changes):
                self.assertIsNone(player_reference({**data, "player": player(**changes)}, True, 12001))

    def test_null_player_retains_expired_center_without_measuring_or_predicting_it(self):
        model = OverlayState()
        model.accept(positioned(), 12001)
        self.assertIsNotNone(model.follow_target(12001))
        model.accept({**positioned(), "player": None}, 12002)
        self.assertIsNotNone(model.player(12002))
        self.assertFalse(model.player(12002)["fresh"])
        self.assertIsNotNone(model.follow_target(12002))
        self.assertEqual(model.follow_target(12002)["x"], player()["x"])
        self.assertIsNone(approximate_distance(model.player(12002), record()))
        model.accept(positioned(), 12003)
        self.assertIsNotNone(model.follow_target(12003))
        model.disconnect()
        self.assertIsNotNone(model.follow_target(12003))
        self.assertFalse(model.follow_target(12003)["fresh"])
        model.accept(snapshot(session_id="restarted"), 12004)
        self.assertIsNone(model.player(12004))

    def test_fresh_follow_keeps_view_at_player_when_a_new_bag_is_elsewhere(self):
        model = OverlayState()
        model.accept(positioned(), 12001)
        new = record("B", map_id=1010, x=200, y=300)
        data = {**positioned(), "records": [new], "alert_seq": 1, "latest_alert": {"record_id": "B"}}
        result = model.accept(data, 12002)
        self.assertEqual(result["alert"]["id"], "B")
        self.assertEqual(model.selected, "B")
        self.assertEqual(model.map_id, 1110)
        self.assertIsNotNone(result["player"])
        model.set_follow(False)
        result = model.accept({**data, "alert_seq": 2}, 12003)
        self.assertEqual(model.map_id, 1010)
        self.assertIsNone(result["player"])

    def test_distance_is_planar_metres_only_with_fresh_same_map_position(self):
        reference = player_reference(positioned(), True, 12001)
        self.assertAlmostEqual(approximate_distance(reference, record()), 5)
        self.assertIsNone(approximate_distance(reference, record(map_id=1010)))
        self.assertIsNone(approximate_distance(reference, record(x=float("inf"))))
        self.assertIsNone(approximate_distance({**reference, "fresh": False}, record()))

    def test_locked_reference_stays_centered_through_manual_controls_until_explicitly_unlocked(self):
        view = headless_view()
        view.state.accept(positioned(), 12001)
        view.center_on_player(view.state.follow_target(12001))
        reference = (player()["x"], player()["y"])
        view.fit(manual=False)
        self.assertTrue(view.state.follow)
        view.fit()
        self.assertTrue(view.state.follow)
        self.assertEqual((view.cx, view.cy), reference)
        view.locate(record("elsewhere", map_id=1010))
        self.assertEqual(view.state.selected, "elsewhere")
        self.assertEqual(view.state.map_id, 1110)
        view.locate_latest()
        view.begin_pan(SimpleNamespace(x=10, y=10))
        view.pan(SimpleNamespace(x=30, y=10))
        self.assertTrue(view.state.follow)
        self.assertEqual((view.cx, view.cy), reference)
        view.zoom(1.5, 7, 11)
        self.assertEqual(view.screen(*reference), (view.width / 2, view.height / 2))
        self.assertTrue(view.state.follow)
        view.toggle_follow()
        self.assertFalse(view.state.follow)
        view.begin_pan(SimpleNamespace(x=10, y=10))
        view.pan(SimpleNamespace(x=30, y=10))
        self.assertNotEqual((view.cx, view.cy), reference)
        view.locate(record("elsewhere", map_id=1010))
        self.assertEqual(view.state.map_id, 1010)
        view.toggle_follow()
        self.assertTrue(view.state.follow)
        self.assertEqual(view.state.map_id, 1110)
        self.assertEqual(view.screen(*reference), (view.width / 2, view.height / 2))

    def test_stale_or_missing_reference_never_allows_alerts_to_move_locked_map(self):
        for initial in (snapshot(), positioned()):
            with self.subTest(has_player="player" in initial):
                model = OverlayState()
                model.accept(initial, 12001)
                distant = record("other", map_id=1010)
                data = {**initial, "player": None, "alert_seq": 1,
                        "latest_alert": {"record_id": "other"}, "records": [distant]}
                result = model.accept(data, 12020)
                self.assertEqual(model.map_id, 1110)
                self.assertEqual(model.selected, "other")
                self.assertEqual(result["alert"], distant)
                if result["player"]:
                    self.assertFalse(result["player"]["fresh"])

    def test_map_changes_unknown_maps_and_sessions_clear_previous_reference(self):
        model = OverlayState()
        model.accept(positioned(), 12001)
        model.accept({**positioned(), "current_map_id": 1010}, 12002)
        self.assertEqual(model.map_id, 1010)
        self.assertIsNone(model.last_player)
        self.assertIsNone(model.player(12002))
        model.accept({**positioned(), "player": None}, 12003)
        self.assertEqual(model.map_id, 1110)
        self.assertIsNone(model.player(12003))
        model.accept(positioned(), 12004)
        model.accept({**positioned(), "current_map_id": None}, 12005)
        self.assertIsNone(model.player(12005))
        self.assertIsNone(model.last_player)
        model.accept({**positioned(), "player": None}, 12006)
        self.assertIsNone(model.follow_target(12006))
        model.accept(positioned(), 12007)
        model.accept(snapshot(session_id="next", player=None), 12008)
        self.assertIsNone(model.follow_target(12008))

    def test_follow_migrates_once_and_zoom_preference_survives_restart(self):
        view = headless_view()
        view.root = SimpleNamespace(winfo_x=lambda: 50, winfo_y=lambda: 70)
        view.alpha, view.collapsed, view.layer = .75, False, False
        with tempfile.TemporaryDirectory() as directory:
            view.settings_path = Path(directory) / "overlay-settings.json"
            view.settings_path.write_text(json.dumps({"follow": False, "zoom_ratio": 12, "alpha": .75}), encoding="utf-8")
            old = load_settings(view.settings_path)
            self.assertTrue(old["follow"])
            self.assertEqual(old["zoom_ratio"], 12)
            view.state.set_follow(old["follow"])
            view.zoom_ratio = old["zoom_ratio"]
            DesktopOverlay.save_settings(view)
            migrated = load_settings(view.settings_path)
            self.assertEqual(migrated["follow_centering_version"], 1)
            view.state.set_follow(False)
            DesktopOverlay.save_settings(view)
            next_launch = load_settings(view.settings_path)
            self.assertFalse(next_launch["follow"])
            self.assertEqual(next_launch["zoom_ratio"], 12)
            self.assertEqual(next_launch["alpha"], .75)
            for invalid in (-1, .49, 40.01, float("nan"), True, "20"):
                view.settings_path.write_text(json.dumps({"zoom_ratio": invalid}), encoding="utf-8")
                self.assertNotIn("zoom_ratio", load_settings(view.settings_path))

    def test_first_real_reference_uses_nearby_zoom_and_restores_user_zoom_after_waiting(self):
        for saved, expected in ((None, 20), (12, 12)):
            with self.subTest(saved=saved):
                view = headless_view()
                view.zoom_ratio = saved
                view.state.accept(snapshot(), 12000)
                view.fit(manual=False)
                self.assertEqual(view.scale / view.fit_scale, 1)
                self.assertIsNone(view.state.player(12000))
                view.state.accept(positioned(), 12001)
                view.center_on_player(view.state.follow_target(12001))
                self.assertEqual(view.scale / view.fit_scale, expected)
                view.zoom(1.3, 20, 30)
                preferred = view.zoom_ratio
                view.state.accept(snapshot(current_map_id=1010), 12002)
                view.fit(manual=False)
                self.assertEqual(view.scale / view.fit_scale, 1)
                view.state.accept({**positioned(), "current_map_id": 1010, "player": player(map_id=1010)}, 12003)
                view.center_on_player(view.state.follow_target(12003))
                self.assertAlmostEqual(view.scale / view.fit_scale, preferred)

    def test_follow_preserves_zoom_when_observation_moves_or_canvas_resizes(self):
        view = headless_view()
        view.state.accept(positioned(), 12001)
        view.center_on_player(player())
        old_scale = view.scale
        view.center_on_player(player(x=111111, y=222222))
        self.assertEqual((view.cx, view.cy), (111111, 222222))
        self.assertEqual(view.scale, old_scale)
        self.assertAlmostEqual(view.scale / view.fit_scale, 4)
        with patch("aion2_redbag_map.overlay.time.time", return_value=12001):
            view.resize(SimpleNamespace(width=360, height=200))
        self.assertEqual((view.cx, view.cy), (player()["x"], player()["y"]))
        self.assertAlmostEqual(view.scale / view.fit_scale, 4)

    def test_bag_focus_never_zooms_out_and_shows_local_terrain_without_a_reference(self):
        for follow in (True, False):
            for existing in (.002, .03):
                with self.subTest(follow=follow, scale=existing):
                    view = headless_view()
                    view.state.set_follow(follow)
                    view.state.accept(snapshot(), 12001)
                    view.scale = existing
                    view.locate(record(), manual=False)
                    self.assertGreaterEqual(view.scale, existing)
                    self.assertLessEqual(min(view.width, view.height) / view.scale, 24000)
                    self.assertEqual((view.cx, view.cy), (record()["x"], record()["y"]))
                    self.assertEqual(view.state.follow, follow)

    def test_live_alert_expands_folded_window_and_keeps_fresh_or_expired_player_centered(self):
        for at in (12001, 12020):
            with self.subTest(at=at):
                view = headless_view()
                view.state.accept(positioned(), 12000)
                view.zoom_ratio = 24
                view.center_on_player(view.state.follow_target(at))
                scale = view.scale
                view.collapsed = True
                view.toggle_collapse = Mock(side_effect=lambda: setattr(view, "collapsed", False))
                bag = record("new", x=150000, y=180000, status="seen", last_seen=at)
                update = view.state.accept(positioned(rows=[bag], alert_seq=1, latest_alert={"record_id": "new"}), at)
                with patch("aion2_redbag_map.overlay.time.time", return_value=at):
                    view.apply_update(update)
                self.assertFalse(view.collapsed)
                view.toggle_collapse.assert_called_once()
                self.assertEqual(view.state.selected, "new")
                self.assertGreaterEqual(view.scale, scale)
                self.assertEqual(view.screen(player()["x"], player()["y"]), (180, 150))
                self.assertTrue(view.state.follow)
                next_update = view.state.accept(view.state.snapshot, at + .2)
                with patch("aion2_redbag_map.overlay.time.time", return_value=at + .2):
                    view.apply_update(next_update)
                self.assertEqual(view.toggle_collapse.call_count, 1)

    def test_history_load_does_not_expand_window_or_replay_focus_alert(self):
        view = headless_view()
        view.collapsed = True
        view.toggle_collapse = Mock()
        view.locate = Mock()
        result = view.state.accept(snapshot(alert_seq=5, latest_alert={"record_id": "A"}), 12001)
        view.apply_update(result)
        view.toggle_collapse.assert_not_called()
        view.locate.assert_not_called()

    def test_screen_edge_indicator_covers_all_directions_and_preserves_the_target_ray(self):
        import math
        for x, y in ((-100, 150), (800, 150), (180, -900), (180, 1200),
                     (-800, -900), (800, -900), (-800, 900), (800, 900)):
            with self.subTest(x=x, y=y):
                hit = target_indicator(x, y, 360, 300)
                self.assertTrue(hit["outside"])
                self.assertTrue(34 <= hit["x"] <= 326)
                self.assertTrue(34 <= hit["y"] <= 266)
                self.assertAlmostEqual(math.hypot(hit["ux"], hit["uy"]), 1)
                self.assertAlmostEqual((hit["x"] - 180) * (y - 150), (hit["y"] - 150) * (x - 180))
        hit = target_indicator(200, 180, 360, 300)
        self.assertFalse(hit["outside"])
        self.assertEqual((hit["x"], hit["y"]), (200, 180))
        self.assertFalse(target_indicator(180, 150, 360, 300)["outside"])
        for x, y in ((340, 150), (20, 150), (180, 20), (180, 280)):
            hit = target_indicator(x, y, 360, 300)
            self.assertFalse(hit["outside"], "a point inside the panel must not become an off-screen arrow")
            self.assertEqual((hit["x"], hit["y"]), (x, y))

    def test_map_change_and_alert_in_same_snapshot_keep_the_existing_close_scale(self):
        view = headless_view()
        view.state.accept(snapshot(), 12000)
        view.scale = .03
        bag = record("new-map-bag", map_id=1010, x=30000, y=40000, status="seen")
        result = view.state.accept(snapshot([bag], current_map_id=1010, alert_seq=1,
                                            latest_alert={"record_id": bag["id"]}), 12001)
        self.assertTrue(result["fit"])
        view.apply_update(result)
        self.assertEqual(view.scale, .03)
        self.assertEqual((view.cx, view.cy), (bag["x"], bag["y"]))
        self.assertEqual(view.state.map_id, 1010)

    def test_map_or_session_change_clears_old_alert_highlight(self):
        for changes in ({"current_map_id": 1010}, {"session_id": "next"}):
            with self.subTest(changes=changes):
                model = OverlayState()
                model.accept(snapshot(), 12000)
                model.accept(snapshot(alert_seq=1, latest_alert={"record_id": "A"}), 12001)
                self.assertEqual(model.alert_until, 12021)
                model.accept(snapshot(alert_seq=1, **changes), 12002)
                self.assertEqual(model.alert_until, 0)

    def test_other_map_alert_cannot_leave_old_map_center_after_a_map_change(self):
        view = headless_view()
        view.state.accept(positioned(), 12000)
        result = view.state.accept(snapshot([record("old-map")], current_map_id=1010, alert_seq=1,
                                            latest_alert={"record_id": "old-map"}), 12001)
        view.apply_update(result)
        self.assertEqual(view.state.map_id, 1010)
        self.assertEqual((view.cx, view.cy), (50000, 50000))
        self.assertIsNone(view.state.player())

    def test_bag_button_keeps_the_alert_target_when_another_record_updates_later(self):
        view = headless_view()
        view.state.set_follow(False)
        target = record("alert", last_seen=10001)
        other = record("other", last_seen=12000)
        view.state.accept(snapshot([target, other]), 12001)
        view.state.selected = "alert"
        view.locate = Mock()
        view.locate_latest()
        view.locate.assert_called_once_with(target)

    def test_status_exposes_own_view_and_freshness_without_entity_identifiers(self):
        view = headless_view()
        view.state.accept({**positioned(), "player": player(entity_id=123)}, 12001)
        view.stopping = threading.Event()
        view.port, view.language = 18726, "zh-CN"
        view.collapsed = view.layer = False
        view.alpha = .94
        view.window_flags = {"hwnd": 101, "no_activate": True}
        view.root = SimpleNamespace(geometry=lambda: "360x454+10+10")
        with tempfile.TemporaryDirectory() as directory:
            view.status_path = Path(directory) / "overlay-status.json"
            with patch("aion2_redbag_map.overlay.time.time", return_value=12001):
                view.write_status()
            status = json.loads(view.status_path.read_text("utf-8"))
        self.assertTrue(status["following"])
        self.assertTrue(status["player_available"])
        self.assertTrue(status["player_fresh"])
        self.assertEqual(status["player_observed_at"], 12000)
        self.assertEqual(status["player_map_id"], 1110)
        self.assertEqual((status["cx"], status["cy"]), (view.cx, view.cy))
        self.assertEqual(status["overlay_version"], "1.3.0")
        self.assertEqual(status["poll_interval_ms"], 200)
        self.assertEqual(status["ui_tick_ms"], 100)
        self.assertEqual(status["base_map_mode"], "overview")
        self.assertNotIn("entity_id", json.dumps(status))

    def test_stop_only_writes_own_file_without_starting_gui(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, "argv", ["overlay.py", "--data-dir", directory, "--stop"]):
                with patch("aion2_redbag_map.overlay.DesktopOverlay") as ui:
                    main()
                    ui.assert_not_called()
            stop = json.loads((Path(directory) / "overlay.stop").read_text("utf-8"))
            self.assertGreater(stop["requested_at"], 0)

    def test_high_detail_render_loads_only_visible_tiles_and_matches_world_position(self):
        bounds = {"x": 0, "y": 0, "w": 200, "h": 200, "tiles": [2, 2]}
        base = Image.new("RGB", (20, 20), "blue")
        loader = Mock(return_value=Image.new("RGB", (100, 100), "green"))
        frame = render_map_frame(base, bounds, 150, 50, 1, 50, 50, loader)
        loader.assert_called_once_with(1, 0)
        self.assertEqual(frame.size, (50, 50))
        self.assertEqual(frame.getextrema(), ((0, 0), (128, 128), (0, 0)))
        self.assertEqual(list(visible_tiles(bounds, -100, -100, 1, 20, 20)), [])

    def test_fractional_pan_and_zoom_keep_tile_edges_aligned_without_seams(self):
        bounds = {"x": 0, "y": 0, "w": 200, "h": 200, "tiles": [2, 2]}
        colors = {(0, 0): (200, 40, 40), (1, 0): (40, 200, 40),
                  (0, 1): (40, 40, 200), (1, 1): (200, 200, 40)}
        tiles = {key: Image.new("RGB", (100, 100), value) for key, value in colors.items()}
        cx, cy, scale, width, height = 100.4, 99.8, .73, 57, 61
        frame = render_map_frame(Image.new("RGB", (20, 20), "black"), bounds, cx, cy, scale,
                                 width, height, lambda col, row: tiles[(col, row)])
        for y in range(height):
            for x in range(width):
                col = int((cx + (x + .5 - width / 2) / scale) >= 100)
                row = int((cy + (y + .5 - height / 2) / scale) >= 100)
                self.assertEqual(frame.getpixel((x, y)), colors[(col, row)], (x, y))

    def test_missing_tiles_fall_back_to_overview_and_small_scale_skips_tiles(self):
        bounds = {"x": 0, "y": 0, "w": 200, "h": 200, "tiles": [2, 2]}
        blue, red = (0, 0, 255), (255, 0, 0)
        loader = lambda col, row: None if (col, row) == (1, 1) else Image.new("RGB", (100, 100), red)
        frame = render_map_frame(Image.new("RGB", (20, 20), blue), bounds, 100, 100, 1, 100, 100, loader)
        self.assertEqual(frame.getpixel((75, 75)), blue)
        self.assertEqual(frame.getpixel((25, 75)), red)
        loader = Mock()
        render_map_frame(Image.new("RGB", (2048, 2048), blue), bounds, 100, 100, .1, 20, 20, loader)
        loader.assert_not_called()

    def test_tile_cache_is_bounded_and_missing_tiles_are_safe(self):
        view = headless_view()
        view.Image = Image
        view.tile_photos = OrderedDict()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "static" / "maps" / "1110"
            folder.mkdir(parents=True)
            for col in range(TILE_CACHE_LIMIT + 1):
                Image.new("RGB", (4, 4), (col, 0, 0)).save(folder / f"{col}_0.webp", lossless=True)
            with patch("aion2_redbag_map.overlay.ROOT", root):
                first = view.tile_image(0, 0)
                self.assertIs(view.tile_image(0, 0), first)
                for col in range(1, TILE_CACHE_LIMIT + 1):
                    view.tile_image(col, 0)
                self.assertEqual(len(view.tile_photos), TILE_CACHE_LIMIT)
                self.assertNotIn((1110, 0, 0), view.tile_photos)
                self.assertIsNone(view.tile_image(99, 99))
                self.assertEqual(len(view.tile_photos), TILE_CACHE_LIMIT)

    def test_fast_poll_queue_preserves_newest_alert_even_if_connection_then_fails(self):
        events = queue.Queue(maxsize=2)
        model = OverlayState()
        model.accept(snapshot(), 12000)
        queue_latest_event(events, (True, snapshot()))
        observed = snapshot(alert_seq=1, latest_alert={"record_id": "A"})
        queue_latest_event(events, (True, observed))
        queue_latest_event(events, (True, {**observed, "player": player()}))
        queue_latest_event(events, (False, None))
        queue_latest_event(events, (False, None))
        self.assertEqual(events.qsize(), 2)
        success, newest = events.get_nowait()
        self.assertTrue(success)
        self.assertEqual(newest["player"], player())
        self.assertEqual(model.accept(newest, 12001)["alert"]["id"], "A")
        self.assertEqual(events.get_nowait(), (False, None))


if __name__ == "__main__":
    unittest.main()
