"""Validate the shipped sound and playback boundary without playing audio."""
from array import array
import math
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).resolve().parents[1] / "src/aion2_redbag_map"
sys.path.insert(0, str(ROOT.parent))
from aion2_redbag_map.notify import Notifier


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

    def test_playback_uses_absolute_file_and_reports_acceptance(self):
        notifier = Notifier("http://127.0.0.1:18726")
        fake = self.fake_winsound()
        with patch.dict(sys.modules, {"winsound": fake}):
            self.assertTrue(notifier._play_sound())
        fake.PlaySound.assert_called_once_with(str(ROOT / "static" / "alert.wav"), 0x20003)
        self.assertTrue(notifier.sound_path.is_absolute())
        self.assertTrue(notifier.status["sound_last_accepted"])
        self.assertEqual(notifier.status["sound_requested"], 1)
        self.assertIsNotNone(notifier.status["sound_last_at"])
        self.assertIsNone(notifier.status["sound_error"])

    def test_playback_failure_is_visible_without_system_chime_fallback(self):
        notifier = Notifier("http://127.0.0.1:18726")
        fake = self.fake_winsound(RuntimeError("audio device unavailable"))
        with patch.dict(sys.modules, {"winsound": fake}), self.assertLogs(level="ERROR"):
            self.assertFalse(notifier._play_sound())
        self.assertEqual(fake.PlaySound.call_count, 1)
        self.assertFalse(notifier.status["sound_last_accepted"])
        self.assertIn("RuntimeError", notifier.status["sound_error"])

    def test_missing_file_is_reported_and_a_later_request_can_recover(self):
        notifier = Notifier("http://127.0.0.1:18726")
        original_path = notifier.sound_path
        notifier.sound_path = ROOT / "static" / "missing-test-alarm.wav"
        fake = self.fake_winsound()
        with patch.dict(sys.modules, {"winsound": fake}):
            with self.assertLogs(level="ERROR"):
                self.assertFalse(notifier._play_sound())
            fake.PlaySound.assert_not_called()
            self.assertIn("FileNotFoundError", notifier.status["sound_error"])
            notifier.sound_path = original_path
            self.assertTrue(notifier._play_sound())
        self.assertTrue(notifier.status["sound_last_accepted"])
        self.assertIsNone(notifier.status["sound_error"])
        self.assertEqual(notifier.status["sound_requested"], 2)


if __name__ == "__main__":
    unittest.main()
