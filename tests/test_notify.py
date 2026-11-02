"""Validate the shipped sound and playback boundary without playing audio."""
from array import array
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).resolve().parents[1] / "src/aion2_redbag_map"
sys.path.insert(0, str(ROOT.parent))
from aion2_redbag_map.notify import Notifier
from aion2_redbag_map import audio


class AlarmTests(unittest.TestCase):
    def test_shipped_alarm_has_three_distinct_groups_without_clipping(self):
        with wave.open(str(ROOT / "static" / "alert.wav"), "rb") as source:
            self.assertEqual(source.getnchannels(), 1)
            self.assertEqual(source.getsampwidth(), 2)
            self.assertEqual(source.getframerate(), 44100)
            rate = source.getframerate()
            pcm = array("h", source.readframes(source.getnframes()))
        if sys.byteorder != "little":
            pcm.byteswap()
        self.assertAlmostEqual(len(pcm) / rate, 3.24, places=3)
        self.assertLess(max(abs(value) for value in pcm), 32767)
        self.assertGreater(max(abs(value) for value in pcm), 0.85 * 32767)
        self.assertEqual(pcm[0], 0)
        self.assertEqual(pcm[-1], 0)
        for start in (1.0, 2.12):
            gap = pcm[round(start * rate):round((start + 0.12) * rate)]
            self.assertTrue(gap and all(value == 0 for value in gap))
        # Each group's four notes alternate the two intended fundamentals.
        for group_start in (0.0, 1.12, 2.24):
            for note in range(4):
                start = round((group_start + note * 0.25 + 0.04) * rate)
                values = pcm[start:start + round(0.15 * rate)]
                rms = math.sqrt(sum(value * value for value in values) / len(values))
                self.assertGreater(rms / 32767, 0.40)
                energies = []
                for frequency in (880, 1320):
                    real = sum(value * math.cos(2 * math.pi * frequency * i / rate)
                               for i, value in enumerate(values))
                    imag = sum(value * math.sin(2 * math.pi * frequency * i / rate)
                               for i, value in enumerate(values))
                    energies.append(math.hypot(real, imag))
                self.assertGreater(energies[note % 2], energies[1 - note % 2] * 10)

    def fake_winsound(self, error=None):
        return types.SimpleNamespace(PlaySound=Mock(side_effect=error),
                                     SND_FILENAME=0x20000, SND_ASYNC=1, SND_NODEFAULT=2)

    def test_default_playback_uses_scaled_chime_cache_and_reports_acceptance(self):
        with tempfile.TemporaryDirectory() as directory:
            notifier = Notifier("http://127.0.0.1:18726", cache_directory=directory)
            fake = self.fake_winsound()
            with patch.dict(sys.modules, {"winsound": fake}):
                self.assertTrue(notifier._play_sound())
            filename, flags = fake.PlaySound.call_args.args
            self.assertEqual(flags, 0x20003)
            self.assertEqual(Path(filename).parent, Path(directory).resolve())
            self.assertEqual(Path(filename).read_bytes(), audio.wav_bytes("chime", 35))
            self.assertEqual((notifier.sound_profile, notifier.sound_volume), ("chime", 35))
            self.assertTrue(notifier.status["sound_last_accepted"])
            self.assertEqual(notifier.status["sound_requested"], 1)
            self.assertEqual(notifier.status["sound_last_profile"], "chime")
            self.assertEqual(notifier.status["sound_last_volume"], 35)
            self.assertIsNotNone(notifier.status["sound_last_at"])
            self.assertIsNone(notifier.status["sound_error"])

    def test_settings_are_strict_and_do_not_enqueue_or_play_sound(self):
        notifier = Notifier("http://127.0.0.1:18726")
        with patch.object(audio, "play") as play:
            notifier.set_sound_settings("bubble", 24)
            self.assertEqual((notifier.sound_profile, notifier.sound_volume), ("bubble", 24))
            self.assertEqual(notifier.status["sound_profile"], "bubble")
            self.assertEqual(notifier.status["sound_volume"], 24)
            self.assertTrue(notifier.queue.empty())
            before = dict(notifier.status)
            for profile, volume in (("missing", 35), (None, 35), ("alarm", True),
                                    ("chime", -1), ("chime", 101), ("chime", 35.0)):
                with self.subTest(profile=profile, volume=volume):
                    with self.assertRaises(ValueError):
                        notifier.set_sound_settings(profile, volume)
                    with self.assertRaises(ValueError):
                        notifier.preview_sound(profile, volume)
                    self.assertEqual(notifier._sound_settings, ("bubble", 24))
                    self.assertEqual(notifier.status, before)
                    self.assertTrue(notifier.queue.empty())
            play.assert_not_called()

    def test_playback_failure_is_visible_without_system_chime_fallback(self):
        notifier = Notifier("http://127.0.0.1:18726")
        notifier.set_sound_settings("arcade", 10)
        with patch.object(audio, "play", side_effect=RuntimeError("audio device unavailable")) as play, self.assertLogs(level="ERROR"):
            self.assertFalse(notifier._play_sound())
        play.assert_called_once_with("arcade", 10, notifier.sound_cache_directory)
        self.assertFalse(notifier.status["sound_last_accepted"])
        self.assertIn("RuntimeError", notifier.status["sound_error"])

    def test_cache_failure_is_reported_and_a_later_request_can_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "cache"
            cache.write_text("not a directory", encoding="utf-8")
            notifier = Notifier("http://127.0.0.1:18726", cache_directory=cache)
            notifier.set_sound_settings("alarm", 50)
            fake = self.fake_winsound()
            with patch.dict(sys.modules, {"winsound": fake}):
                with self.assertLogs(level="ERROR"):
                    self.assertFalse(notifier._play_sound())
                fake.PlaySound.assert_not_called()
                self.assertIn("FileExistsError", notifier.status["sound_error"])
                cache.unlink()
                self.assertTrue(notifier._play_sound())
            self.assertTrue(notifier.status["sound_last_accepted"])
            self.assertIsNone(notifier.status["sound_error"])
            self.assertEqual(notifier.status["sound_requested"], 2)
            filename, flags = fake.PlaySound.call_args.args
            self.assertEqual(Path(filename).read_bytes(), audio.wav_bytes("alarm", 50))
            self.assertEqual(flags, 0x20003)

    def test_queue_freezes_settings_and_alert_contents_at_enqueue(self):
        notifier = Notifier("http://127.0.0.1:18726")
        alert = {"title": "First", "text": "X 10 / Y 20"}
        notifier.send(alert)
        alert["text"] = "changed later"
        notifier.set_sound_settings("alarm", 50)
        notifier.send({"title": "Second"}, sound=False)
        self.assertTrue(notifier.preview_sound("bubble", 20))
        notifier.set_sound_settings("sonar", 5)
        balloon = Mock()
        with patch.object(notifier, "_play_sound") as play:
            for _ in range(3):
                notifier._deliver_request(notifier.queue.get_nowait(), balloon)
        self.assertEqual([call.args for call in play.call_args_list], [("chime", 35), ("bubble", 20)])
        self.assertEqual([call.args for call in balloon.call_args_list],
                         [({"title": "First", "text": "X 10 / Y 20"},), ({"title": "Second"},)])

    def test_preview_only_plays_sound_and_never_sends_balloon(self):
        notifier = Notifier("http://127.0.0.1:18726")
        balloon = Mock()
        self.assertTrue(notifier.preview_sound("bell", 15))
        with patch.object(notifier, "_play_sound") as play:
            notifier._deliver_request(notifier.queue.get_nowait(), balloon)
        play.assert_called_once_with("bell", 15)
        balloon.assert_not_called()
        self.assertEqual(notifier.status["sent"], 0)
        self.assertIsNone(notifier.status["last_at"])
        self.assertEqual(notifier._sound_settings, ("chime", 35))

    def test_volume_zero_skips_sound_for_alerts_and_previews(self):
        notifier = Notifier("http://127.0.0.1:18726")
        notifier.set_sound_settings("chime", 0)
        notifier.send({"title": "Visual only"})
        self.assertTrue(notifier.preview_sound("alarm", 0))
        balloon = Mock()
        with patch.object(notifier, "_play_sound") as play:
            notifier._deliver_request(notifier.queue.get_nowait(), balloon)
            notifier._deliver_request(notifier.queue.get_nowait(), balloon)
        play.assert_not_called()
        balloon.assert_called_once_with({"title": "Visual only"})
        with patch.object(audio, "play") as play:
            self.assertFalse(notifier._play_sound())
        play.assert_not_called()
        self.assertEqual(notifier.status["sound_requested"], 0)

    def test_full_queue_rejects_preview_without_erasing_existing_alerts(self):
        notifier = Notifier("http://127.0.0.1:18726")
        for index in range(64):
            notifier.send({"index": index}, sound=False)
        self.assertFalse(notifier.preview_sound("bell", 5))
        self.assertEqual(notifier.queue.qsize(), 64)
        self.assertIsNotNone(notifier.status["error"])
        self.assertEqual(notifier.queue.get_nowait()[0], {"index": 0})

    def test_preview_playback_failure_preserves_visual_notification_counters(self):
        notifier = Notifier("http://127.0.0.1:18726")
        self.assertTrue(notifier.preview_sound("bell", 15))
        balloon = Mock()
        with patch.object(audio, "play", side_effect=RuntimeError("device unavailable")):
            with self.assertLogs(level="ERROR"):
                notifier._deliver_request(notifier.queue.get_nowait(), balloon)
        self.assertFalse(notifier.status["sound_last_accepted"])
        self.assertIn("RuntimeError", notifier.status["sound_error"])
        self.assertEqual(notifier.status["sent"], 0)
        self.assertIsNone(notifier.status["last_at"])
        balloon.assert_not_called()


if __name__ == "__main__":
    unittest.main()
