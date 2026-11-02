"""Alert choices persist without starting reception or changing observations."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map import alert_settings
from aion2_redbag_map.runtime import RuntimeController, DesktopError


class AlertPreferenceTests(unittest.TestCase):
    def test_first_live_observation_during_service_start_is_not_lost(self):
        from aion2_redbag_map.model import MapState
        from aion2_redbag_map.overlay import OverlayState, AlertVisibility
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            alert_settings.save(root, {"overlay_on_alert": True})
            model = MapState(root, [{"id": 1110, "name": "Altgard"}], mode="live")
            model.capture_status({"phase": "observing"})
            client = Mock(url="http://127.0.0.1:49173")
            client.snapshot.return_value = None
            service, overlay = Mock(pid=123), Mock(pid=456)
            service.poll.return_value = overlay.poll.return_value = None
            def create(command, **_options):
                if command[command.index('--worker') + 1] == 'service':
                    model.handle({"kind": "object", "at": 1793600000, "entity_id": 1234,
                                  "template_id": 1301199, "x": 100, "y": 200, "z": 3,
                                  "states": [1, 1, 6]})
                    client.snapshot.return_value = dict(model.snapshot(), process_id=123)
                    return service
                return overlay
            factory = Mock(side_effect=create)
            manager = RuntimeController(data_dir=root, client=client, process_factory=factory,
                                        driver_check=lambda: True, sleep=lambda _s: None)
            current = manager.start_receiving()
            self.assertEqual(current['alert_seq'], 1)
            command = factory.call_args.args[0]
            baseline = int(command[command.index('--alert-baseline-seq') + 1])
            self.assertEqual(baseline, 0)
            state = OverlayState(baseline_session=current['session_id'], baseline_seq=baseline)
            visibility = AlertVisibility(True, baseline_session=current['session_id'], baseline_seq=baseline)
            change = state.accept(current)
            self.assertIsNotNone(change['alert'])
            self.assertEqual(visibility.accept(current, change['alert']), 'show')

    def test_hidden_worker_launch_is_single_and_manual_show_reuses_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            client = Mock(url="http://127.0.0.1:49173")
            state = {"process_id": 123, "session_id": "session-new", "alert_seq": 3, "mode": "live"}
            client.snapshot.return_value = state
            child = Mock(pid=456)
            child.poll.return_value = None
            entered, release = threading.Event(), threading.Event()
            def create(*args, **kwargs):
                entered.set()
                self.assertTrue(release.wait(2))
                return child
            factory = Mock(side_effect=create)
            manager = RuntimeController(data_dir=root, client=client, process_factory=factory)
            with ThreadPoolExecutor(max_workers=2) as pool:
                one = pool.submit(manager._ensure_alert_overlay, state, True)
                self.assertTrue(entered.wait(2))
                two = pool.submit(manager._ensure_alert_overlay, state, True)
                release.set()
                one.result(timeout=2)
                two.result(timeout=2)
            factory.assert_called_once()
            command = factory.call_args.args[0]
            self.assertIn('--hidden-until-alert', command)
            self.assertEqual(command[command.index('--alert-baseline-session') + 1], 'session-new')
            self.assertEqual(command[command.index('--alert-baseline-seq') + 1], '0')
            manager.show_overlay()
            show = json.loads((root / 'overlay.show').read_text(encoding='utf-8'))
            self.assertEqual(show['pid'], 456)
            self.assertEqual(show['api'], client.url + '/api/state')
            factory.assert_called_once()
            # A late status result after close cannot start another worker.
            manager.overlay = None
            manager.close()
            manager._ensure_alert_overlay(state)
            factory.assert_called_once()

    def test_offline_settings_and_preview_never_start_workers_or_rewrite_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "history.json"
            original = b'{"schema":1,"language":"ja","records":[{"id":"preserve"}]}'
            history.write_bytes(original)
            client = Mock()
            client.snapshot.return_value = None
            factory = Mock(side_effect=AssertionError("must not start a worker"))
            manager = RuntimeController(data_dir=root, client=client, process_factory=factory)
            manager.set_alert_settings(sound_profile="arcade", sound_volume=18, overlay_on_alert=True)
            manager.set_language("en")
            restored = RuntimeController(data_dir=root, client=client, process_factory=factory)
            self.assertEqual(restored.alert_settings,
                {"sound_profile": "arcade", "sound_volume": 18, "overlay_on_alert": True})
            with patch("aion2_redbag_map.runtime.play_sound") as play:
                restored.preview_sound("bubble", 12)
                play.assert_called_once_with("bubble", 12, root.resolve() / "sound-cache")
            self.assertEqual(history.read_bytes(), original)
            factory.assert_not_called()
            client.control.assert_not_called()

    def test_invalid_or_failed_save_does_not_replace_applied_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = alert_settings.save(root, {"sound_profile": "bell", "sound_volume": 24})
            before = (root / "alert-settings.json").read_bytes()
            client = Mock()
            client.snapshot.return_value = None
            manager = RuntimeController(data_dir=root, client=client)
            with self.assertRaises(ValueError):
                manager.set_alert_settings(sound_profile="chime", sound_volume=101)
            with patch("aion2_redbag_map.alert_settings.atomic_write", side_effect=PermissionError("test")):
                with self.assertRaises(OSError):
                    manager.set_alert_settings(sound_profile="sonar")
            self.assertEqual(manager.alert_settings, saved)
            self.assertEqual((root / "alert-settings.json").read_bytes(), before)

    def test_live_control_failure_and_native_preview_do_not_apply_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = Mock()
            client.snapshot.return_value = {"process_id": 123, "mode": "live", "paused": False}
            manager = RuntimeController(data_dir=root, client=client)
            old = dict(manager.alert_settings)
            client.control.side_effect = DesktopError("rejected")
            with self.assertRaises(DesktopError):
                manager.set_alert_settings(sound_volume=70)
            self.assertEqual(manager.alert_settings, old)
            client.control.side_effect = None
            client.control.return_value = {"ok": True}
            with patch("aion2_redbag_map.runtime.play_sound") as fallback:
                manager.preview_sound("sonar", 28)
                fallback.assert_not_called()
            client.control.assert_called_with("preview_sound", expected_pid=123,
                                               sound_profile="sonar", sound_volume=28)
            self.assertEqual(manager.alert_settings, old)

    def test_corrupt_settings_only_fall_back_invalid_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "alert-settings.json").write_text(json.dumps({"sound_profile": "../bad", "sound_volume": 11,
                                                                   "overlay_on_alert": "yes"}), encoding="utf-8")
            self.assertEqual(alert_settings.load(root), {"sound_profile": "chime", "sound_volume": 11,
                                                        "overlay_on_alert": False})


if __name__ == "__main__":
    unittest.main()
