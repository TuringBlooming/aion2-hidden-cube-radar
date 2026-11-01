"""Language changes are local preferences and must never produce bag observations."""
import argparse
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / "src/aion2_redbag_map"
sys.path.insert(0, str(ROOT.parent))
from aion2_redbag_map.app import Application
from aion2_redbag_map.localization import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, map_name, text
from aion2_redbag_map.model import MapState
from aion2_redbag_map.notify import Notifier

MAPS = [{"id": 1110, "name": "阿尔特盖德", "english_name": "Altgard"},
        {"id": 600001, "name": "Caverna de Krao", "english_name": "Caverna de Krao"}]
AT = 1792749938.682


def observation(**extra):
    data = {"kind": "object", "at": AT, "connection_id": "test-flow", "entity_id": 42895,
            "template_id": 1301199, "x": 106518.438, "y": 71755.414, "z": 4347,
            "states": [1, 1, 6], "spawned_at": 1792749938682}
    data.update(extra)
    return data


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_supported_languages_are_validated_and_persisted(self):
        state = MapState(self.directory, MAPS)
        self.assertEqual(state.language, DEFAULT_LANGUAGE)
        self.assertEqual(state.snapshot()["supported_languages"], list(SUPPORTED_LANGUAGES))
        for language in SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                state.set_language(language)
                restored = MapState(self.directory, MAPS)
                self.assertEqual(restored.snapshot()["language"], language)
                self.assertEqual(json.loads(state.path.read_text("utf-8"))["schema"], 1)
        for language in (None, True, 1, [], {}, "zh", "en-US", "zh-cn", "fr", ""):
            with self.subTest(invalid=language):
                before = state.path.read_bytes()
                with self.assertRaises(ValueError):
                    state.set_language(language)
                self.assertEqual(state.language, "ko")
                self.assertEqual(state.path.read_bytes(), before)

    def test_old_or_invalid_preference_defaults_without_losing_history(self):
        state = MapState(self.directory, MAPS)
        state.handle(observation(states=[4, 3, 6]))
        original = json.loads(state.path.read_text("utf-8"))
        for language in (None, "unsupported", ["en"]):
            with self.subTest(language=language):
                saved = copy.deepcopy(original)
                saved.pop("language", None)
                if language is not None:
                    saved["language"] = language
                state.path.write_text(json.dumps(saved), "utf-8")
                restored = MapState(self.directory, MAPS)
                self.assertEqual(restored.language, "zh-CN")
                self.assertEqual(len(restored.records), 1)
                record = next(iter(restored.records.values()))
                self.assertEqual(record["status"], "opened")
                self.assertEqual(record["refresh_anchor_at"], AT)
                self.assertIsNone(restored.storage_error)

    def test_real_alerts_use_language_and_preserve_structured_coordinates(self):
        titles = {"zh-CN": "发现红色背包", "en": "Red bag spotted",
                  "ja": "赤いバッグを発見", "ko": "빨간 가방 발견"}
        names = {"zh-CN": "阿尔特盖德", "en": "Altgard",
                 "ja": "アルトガルド", "ko": "알트가르드"}
        for language in SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                state = MapState(self.directory / language, MAPS)
                state.set_language(language)
                alert = state.handle(observation())
                self.assertEqual(alert["title"], titles[language])
                self.assertEqual(alert["language"], language)
                self.assertIn(names[language], alert["text"])
                self.assertIn("X 106518 / Y 71755", alert["text"])
                self.assertEqual((alert["map_id"], alert["x"], alert["y"], alert["z"]),
                                 (1110, 106518.438, 71755.414, 4347))
                self.assertEqual(state.records[alert["record_id"]]["status"], "seen")
                self.assertIsNone(state.handle(observation(at=AT+1)))
                self.assertEqual(state.alert_seq, 1)

    def test_switch_does_not_rewrite_records_events_timers_or_replay_alerts(self):
        state = MapState(self.directory, MAPS)
        state.handle(observation())
        state.handle(observation(kind="state", a=4, b=3, at=AT+10))
        with patch("aion2_redbag_map.model.time.time", return_value=AT+3310):
            before = state.snapshot()
            for language in SUPPORTED_LANGUAGES:
                state.set_language(language)
                after = state.snapshot()
                for key in ("records", "events", "alert_seq", "latest_alert", "capture",
                            "selected_map_id", "current_map_id", "sound", "paused"):
                    self.assertEqual(after[key], before[key], key)
        self.assertEqual(before["records"][0]["display_status"], "soon")
        self.assertEqual(before["records"][0]["status"], "opened")

    def test_control_persists_language_and_localizes_test_without_real_alert(self):
        args = argparse.Namespace(data_dir=self.tmp.name, port=18799, empty=True,
                                  replay=None, no_capture=True)
        app = Application(args)
        app.notifier.send = Mock()
        for language in SUPPORTED_LANGUAGES:
            with self.subTest(language=language):
                self.assertEqual(app.control({"action": "language", "language": language}),
                                 {"ok": True, "action": "language"})
                self.assertEqual(app.notifier.language, language)
                self.assertEqual(app.snapshot()["language"], language)
                app.control({"action": "test_alert"})
                alert, sound = app.notifier.send.call_args.args
                self.assertEqual(alert["title"], text("test_title", language))
                self.assertEqual(alert["text"], text("test_text", language))
                self.assertEqual(alert["language"], language)
                self.assertTrue(sound)
                self.assertEqual(app.state.alert_seq, 0)
                self.assertEqual(app.state.records, {})
        self.assertIsNone(app.collector)
        self.assertFalse(app.notifier.thread.is_alive())
        for invalid in (None, "fr", True, []):
            with self.assertRaises(ValueError):
                app.control({"action": "language", "language": invalid})
        self.assertEqual(app.notifier.language, "ko")
        restored = Application(args)
        self.assertEqual(restored.notifier.language, "ko")

    def test_map_fallback_and_tray_change_do_not_enqueue_a_sound(self):
        for language in SUPPORTED_LANGUAGES:
            notifier = Notifier("http://127.0.0.1:18799", language)
            for choice in SUPPORTED_LANGUAGES:
                notifier.set_language(choice)
                self.assertEqual(notifier.language, choice)
                self.assertTrue(notifier.queue.empty())
                self.assertEqual(notifier.status["sound_requested"], 0)
                self.assertLess(len(text("tray", choice)), 128)
                self.assertLess(len(text("test_title", choice)), 64)
                self.assertLess(len(text("test_text", choice)), 256)
        self.assertEqual(map_name(MAPS, 600001, "en"), "Krao Cave")
        self.assertEqual(map_name(MAPS, 600001, "ja"), "クラオ洞窟")
        self.assertEqual(map_name(MAPS, 600001, "zh-CN"), "克劳洞穴")
        self.assertEqual(map_name(MAPS, 600001, "ko"), "크라오 동굴")
        self.assertEqual(map_name(MAPS, 600021, "en"), "Fire Temple")
        self.assertEqual(map_name(MAPS, 9999, "ko"), "지도 9999")
        unknown_maps = [{"id": 9999, "name": "新地图", "english_name": "New map"}]
        self.assertEqual(map_name(unknown_maps, 9999, "en"), "New map")
        self.assertEqual(map_name(unknown_maps, 9999, "zh-CN"), "新地图")


if __name__ == "__main__":
    unittest.main()
