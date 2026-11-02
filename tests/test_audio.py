"""Validate cue content, independent gain and playback without producing sound."""
from array import array
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).resolve().parents[1] / "src/aion2_redbag_map"
sys.path.insert(0, str(ROOT.parent))
from aion2_redbag_map import audio


def read_wave(value):
    with wave.open(io.BytesIO(value), "rb") as source:
        params = source.getparams()
        pcm = array("h", source.readframes(source.getnframes()))
    if sys.byteorder != "little":
        pcm.byteswap()
    return params, pcm


class AudioTests(unittest.TestCase):
    def test_all_cues_are_distinct_valid_local_waveforms(self):
        self.assertEqual(audio.DEFAULT_SOUND, "chime")
        self.assertEqual(audio.DEFAULT_VOLUME, 35)
        self.assertEqual(len(audio.SOUND_IDS), 6)
        values = []
        for sound_id in audio.SOUND_IDS:
            with self.subTest(sound=sound_id):
                value = audio.wav_bytes(sound_id, 100)
                values.append(value)
                params, pcm = read_wave(value)
                self.assertEqual((params.nchannels, params.sampwidth, params.framerate), (1, 2, 44100))
                self.assertEqual(params.comptype, "NONE")
                self.assertGreater(params.nframes / params.framerate, .5)
                self.assertLess(params.nframes / params.framerate, 4)
                self.assertEqual((pcm[0], pcm[-1]), (0, 0))
                peak = max(abs(sample) for sample in pcm)
                self.assertGreater(peak, .3 * 32767)
                self.assertLess(peak, 32767)
                if sound_id != "alarm":
                    self.assertLessEqual(peak, .421 * 32767)
        self.assertEqual(len(set(values)), len(audio.SOUND_IDS))

    def test_volume_scales_samples_without_changing_duration_or_pitch(self):
        for sound_id in audio.SOUND_IDS:
            with self.subTest(sound=sound_id):
                full_params, full = read_wave(audio.wav_bytes(sound_id, 100))
                half_params, half = read_wave(audio.wav_bytes(sound_id, 50))
                silent_params, silent = read_wave(audio.wav_bytes(sound_id, 0))
                self.assertEqual(full_params, half_params)
                self.assertEqual(full_params, silent_params)
                self.assertTrue(all(abs(2 * b - a) <= 1 for a, b in zip(full, half)))
                self.assertTrue(all(value == 0 for value in silent))
                default_params, quiet = read_wave(audio.wav_bytes(sound_id, audio.DEFAULT_VOLUME))
                self.assertEqual(default_params, full_params)
                self.assertLess(max(abs(v) for v in quiet), max(abs(v) for v in half))

    def test_original_alarm_is_retained_at_full_volume(self):
        self.assertEqual(audio.wav_bytes("alarm", 100), (ROOT / "static/alert.wav").read_bytes())

    def test_invalid_settings_fail_before_rendering_or_playback(self):
        pairs = [(name, 35) for name in (None, [], {}, True, "", "unknown", "../alarm", "Alarm")]
        pairs += [("chime", value) for value in (-1, 101, True, False, 35., "35", None, [], {})]
        for sound_id, volume in pairs:
            with self.subTest(sound=sound_id, volume=volume):
                with self.assertRaises(ValueError):
                    audio.validate_sound_settings(sound_id, volume)
                with self.assertRaises(ValueError):
                    audio.wav_bytes(sound_id, volume)
                with patch.object(audio, "_cache_wave") as cache:
                    with self.assertRaises(ValueError):
                        audio.play(sound_id, volume, "not-used")
                    cache.assert_not_called()

    def fake_winsound(self, error=None):
        return types.SimpleNamespace(PlaySound=Mock(side_effect=error),
                                     SND_FILENAME=0x20000, SND_ASYNC=1, SND_NODEFAULT=2)

    def test_playback_uses_our_gain_file_and_repairs_corrupted_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = self.fake_winsound()
            with patch.dict(sys.modules, {"winsound": fake}):
                self.assertTrue(audio.play("bubble", 25, directory))
                filename, flags = fake.PlaySound.call_args.args
                path = Path(filename)
                self.assertEqual(path.parent, Path(directory).resolve())
                self.assertEqual(path.read_bytes(), audio.wav_bytes("bubble", 25))
                self.assertEqual(flags, 0x20003)
                self.assertFalse(list(Path(directory).glob("*.tmp")))
                path.write_bytes(b"broken cache")
                self.assertTrue(audio.play("bubble", 25, directory))
                self.assertEqual(path.read_bytes(), audio.wav_bytes("bubble", 25))
                self.assertEqual(len(list(Path(directory).glob("*.wav"))), 1)

    def test_muted_playback_does_not_write_or_submit_sound(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = self.fake_winsound()
            with patch.dict(sys.modules, {"winsound": fake}):
                self.assertFalse(audio.play("chime", 0, directory))
            fake.PlaySound.assert_not_called()
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_playback_errors_are_not_replaced_with_a_system_chime(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = self.fake_winsound(RuntimeError("audio unavailable"))
            with patch.dict(sys.modules, {"winsound": fake}):
                with self.assertRaises(RuntimeError):
                    audio.play("chime", 35, directory)
            fake.PlaySound.assert_called_once()
            self.assertEqual(fake.PlaySound.call_args.args[1], 0x20003)


if __name__ == "__main__":
    unittest.main()
