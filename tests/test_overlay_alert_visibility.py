"""Real-alert visibility and local control, without any desktop, game or audio."""
import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.overlay import (AlertVisibility, DesktopOverlay, OverlayState,
                                     consume_show_command, show_without_activation)


def snapshot(seq=0, **changes):
    row = {"id": "cube", "map_id": 1110, "x": 1000, "y": 2000,
           "status": "seen", "source": "live", "last_seen": 1000}
    return {"app": "AION2 RedBag Map", "session_id": "session-A", "alert_seq": seq,
            "mode": "live", "capture": {"phase": "observing"}, "paused": False,
            "current_map_id": 1110, "overlay_on_alert": True, "now": 1000 + seq,
            "records": [row], "latest_alert": {"record_id": "cube", "seq": seq}, **changes}


class AlertVisibilityTests(unittest.TestCase):
    def pair(self, *, waiting=True, baseline=None):
        args = {} if baseline is None else {"baseline_session": baseline[0], "baseline_seq": baseline[1]}
        return OverlayState(**args), AlertVisibility(waiting, **args)

    def update(self, pair, data):
        state, visibility = pair
        event = state.accept(data)
        return visibility.accept(data, event["alert"])

    def test_history_baseline_then_only_new_live_alert_reveals_hidden_window(self):
        pair = self.pair()
        self.assertIsNone(self.update(pair, snapshot(5)))
        self.assertFalse(pair[1].visible)
        self.assertEqual(self.update(pair, snapshot(6)), "show")
        pair[1].arm()
        self.assertIsNone(self.update(pair, snapshot(6, now=1007)))
        self.assertFalse(pair[1].visible)
        opened = snapshot(6, now=1008)
        opened["records"][0]["status"] = "opened"
        self.assertIsNone(self.update(pair, opened))
        self.assertFalse(pair[1].visible)

    def test_explicit_matching_baseline_preserves_alert_during_worker_startup(self):
        pair = self.pair(baseline=("session-A", 5))
        self.assertEqual(self.update(pair, snapshot(6)), "show")
        pair[1].arm()
        self.assertIsNone(self.update(pair, snapshot(40, session_id="replacement")))
        self.assertFalse(pair[1].visible)
        self.assertEqual(self.update(pair, snapshot(41, session_id="replacement")), "show")

    def test_mismatched_explicit_session_does_not_replay_its_existing_alert(self):
        pair = self.pair(baseline=("previous", 0))
        self.assertIsNone(self.update(pair, snapshot(100)))
        self.assertFalse(pair[1].visible)

    def test_replay_paused_test_and_history_source_are_not_popup_events(self):
        for change in ({"mode": "replay"}, {"paused": True}, {"capture": {"phase": "paused"}},
                       {"latest_alert": {"record_id": "cube", "seq": 1, "test": True}},
                       {"latest_alert": {"record_id": "cube", "seq": 999}}, {"session_id": None}):
            with self.subTest(change=change):
                pair = self.pair()
                self.update(pair, snapshot())
                self.assertIsNone(self.update(pair, snapshot(1, **change)))
                self.assertFalse(pair[1].visible)
        pair = self.pair()
        self.update(pair, snapshot())
        history = snapshot(1)
        history["records"][0]["source"] = "history"
        self.assertIsNone(self.update(pair, history))
        self.assertFalse(pair[1].visible)

    def test_manual_open_works_with_saved_mode_and_setting_changes_hide_or_show(self):
        pair = self.pair(waiting=False)
        self.assertIsNone(self.update(pair, snapshot(5)))
        self.assertTrue(pair[1].visible)
        self.update(pair, snapshot(5, now=1010, overlay_on_alert=False))
        self.assertEqual(self.update(pair, snapshot(5, now=1011)), "hide")
        self.assertEqual(self.update(pair, snapshot(5, now=1012, overlay_on_alert=False)), "show")
        pair[1].manual_show()
        self.update(pair, snapshot(5, now=1013))
        self.assertFalse(pair[1].visible, "An explicit off-to-on setting change still arms it")
        pair[1].manual_show()
        self.update(pair, snapshot(5, now=1014))
        self.assertTrue(pair[1].visible, "Ordinary same-mode snapshots keep a manual open visible")

    def test_late_poll_cannot_undo_acknowledged_hide_setting(self):
        pair = self.pair()
        self.update(pair, snapshot(3, now=1010))
        pair[1].arm()
        self.assertIsNone(self.update(pair, snapshot(3, now=1009, overlay_on_alert=False)))
        self.assertTrue(pair[1].enabled)
        self.assertFalse(pair[1].visible)

    def test_show_command_is_instance_scoped_fresh_and_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "overlay.show"
            good = {"pid": 42, "api": "http://127.0.0.1:18726/api/state", "requested_at": 1001}
            args = {"pid": 42, "api": good["api"], "started_at": 1000, "now": 1002}
            for change in ({"pid": 43}, {"api": "http://127.0.0.1:18727/api/state"},
                           {"requested_at": 999}, {"requested_at": 1010}, {"pid": True}):
                p.write_text(json.dumps({**good, **change}), encoding="utf-8")
                self.assertFalse(consume_show_command(p, **args))
            p.write_text(json.dumps(good), encoding="utf-8")
            self.assertTrue(consume_show_command(p, **args))
            self.assertFalse(p.exists())
            self.assertFalse(consume_show_command(p, **args))

    def test_show_styles_only_the_owned_window_without_focus_calls(self):
        view = DesktopOverlay.__new__(DesktopOverlay)
        view.root = Mock()
        with patch("aion2_redbag_map.overlay.show_without_activation", return_value={"no_activate": True}) as show:
            view.set_visible(True)
        show.assert_called_once_with(view.root)
        self.assertEqual(view.root.mock_calls, [])
        view.set_visible(False)
        view.root.withdraw.assert_called_once()

    def test_native_show_disables_only_owned_wrapper_and_requests_no_activation(self):
        root, user = Mock(), Mock()
        with patch("aion2_redbag_map.overlay.os.name", "nt"), \
             patch("aion2_redbag_map.overlay.ctypes.WinDLL", return_value=user, create=True), \
             patch("aion2_redbag_map.overlay.own_window_style", return_value={"hwnd": 1234}) as style:
            show_without_activation(root)
        user.EnableWindow.assert_has_calls([call(1234, False), call(1234, True)])
        user.ShowWindow.assert_called_once_with(1234, 4)
        style.assert_has_calls([call(root, show=False), call(root, show=True)])
        self.assertEqual(root.mock_calls, [call.deiconify()])
        user.GetForegroundWindow.assert_not_called()
        user.SetForegroundWindow.assert_not_called()

    def test_hide_preference_is_authenticated_and_failure_keeps_window_available(self):
        view = DesktopOverlay.__new__(DesktopOverlay)
        view.port = 49173
        view.control_events = queue.Queue()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [b'{"ok":true}', json.dumps(snapshot(process_id=88)).encode()]
        opener = Mock()
        opener.open.return_value = response
        with patch("aion2_redbag_map.overlay.build_opener", return_value=opener):
            view._save_alert_only("test-only-token", "session-A", 88)
        request = opener.open.call_args_list[0].args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:49173/api/control")
        self.assertEqual(request.get_header("Origin"), "http://127.0.0.1:49173")
        self.assertEqual(request.get_header("X-redbag-key"), "test-only-token")
        self.assertEqual(json.loads(request.data), {"action": "alert_settings", "overlay_on_alert": True})
        self.assertTrue(view.control_events.get_nowait()[0])
        with patch("aion2_redbag_map.overlay.build_opener", side_effect=OSError("offline")):
            view._save_alert_only("test-only-token", "session-A", 88)
        self.assertEqual(view.control_events.get_nowait(), (False, None))


if __name__ == "__main__":
    unittest.main()
