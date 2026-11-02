"""Original local alert sounds and per-alert PCM gain, with no system-volume API.

The same WAV bytes can be served to the browser or played by Windows. Generating
or testing a sound never starts playback. Only ``play`` submits audio, from a
file because winsound does not support asynchronous in-memory playback.
"""
from array import array
from functools import lru_cache
import hashlib
import io
import math
import os
from pathlib import Path
import sys
import tempfile
import wave

SOUND_IDS = ("chime", "bell", "bubble", "arcade", "sonar", "alarm")
DEFAULT_SOUND = "chime"
DEFAULT_VOLUME = 35
RATE = 44100


def validate_sound_settings(sound_id, volume):
    """Return the validated pair, rejecting coercion and path-like sound IDs."""
    if not isinstance(sound_id, str) or sound_id not in SOUND_IDS:
        raise ValueError("Unknown alert sound")
    if type(volume) is not int or not 0 <= volume <= 100:
        raise ValueError("Alert volume must be an integer from 0 to 100")
    return sound_id, volume


def _encode(pcm, rate=RATE, channels=1):
    frames = array("h", pcm)
    if sys.byteorder != "little":
        frames.byteswap()
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(frames.tobytes())
    return output.getvalue()


def _synthesize(sound_id):
    """Compose short, smoothly gated cues from original sine partials."""
    # start, length, fundamental at start/end, partial weights, decay strength
    if sound_id == "chime":
        duration = 1.12
        notes = [(start, .72, pitch, pitch, (1., .08), 4.)
                 for start, pitch in ((0., 523.25), (.18, 659.25), (.36, 783.99))]
    elif sound_id == "bell":
        duration = 1.35
        notes = [(0., 1.3, 587.33, 587.33, (1., .16, .05), 5.),
                 (.22, .95, 880., 880., (.4, .04), 5.)]
    elif sound_id == "bubble":
        duration = .8
        notes = [(0., .2, 220., 700., (1., .08), 2.5),
                 (.29, .24, 300., 880., (1., .08), 2.5)]
    elif sound_id == "arcade":
        duration = .85
        notes = [(i * .14, .2, pitch, pitch, (1., 0., .07), 1.5)
                 for i, pitch in enumerate((523.25, 659.25, 783.99, 1046.5))]
    elif sound_id == "sonar":
        duration = 1.25
        notes = [(0., 1.2, 660., 660., (1., .06), 4.5)]
    else:
        raise ValueError("No synthesized sound for this ID")
    signal = [0.] * round(duration * RATE)
    for start, length, low, high, partials, decay in notes:
        offset = round(start * RATE)
        count = min(round(length * RATE), len(signal) - offset)
        for i in range(count):
            t = i / RATE
            attack = min(1., t / .014)
            release = min(1., (count - 1 - i) / (.045 * RATE))
            gate = (.5 - .5 * math.cos(math.pi * attack)) * (.5 - .5 * math.cos(math.pi * release))
            phase = 2 * math.pi * (low * t + (high - low) * t * t / (2 * length))
            value = sum(weight * math.sin((harmonic + 1) * phase)
                        for harmonic, weight in enumerate(partials))
            signal[offset + i] += value * gate * math.exp(-decay * t / length)
    peak = max(abs(value) for value in signal)
    gain = .42 * 32767 / peak
    return _encode(round(value * gain) for value in signal)


@lru_cache(maxsize=len(SOUND_IDS))
def _base_wave(sound_id):
    if sound_id == "alarm":
        value = (Path(__file__).resolve().parent / "static/alert.wav").read_bytes()
    else:
        value = _synthesize(sound_id)
    with wave.open(io.BytesIO(value), "rb") as source:
        if source.getsampwidth() != 2 or source.getcomptype() != "NONE":
            raise ValueError("Alert sound must be uncompressed 16-bit PCM")
    return value


@lru_cache(maxsize=32)
def _with_gain(sound_id, volume):
    original = _base_wave(sound_id)
    if volume == 100:
        return original
    with wave.open(io.BytesIO(original), "rb") as source:
        rate, channels = source.getframerate(), source.getnchannels()
        pcm = array("h", source.readframes(source.getnframes()))
    if sys.byteorder != "little":
        pcm.byteswap()
    return _encode((round(value * volume / 100) for value in pcm), rate, channels)


def wav_bytes(sound_id=DEFAULT_SOUND, volume=DEFAULT_VOLUME):
    """Return a bounded cached WAV with gain applied to its PCM samples only."""
    sound_id, volume = validate_sound_settings(sound_id, volume)
    return _with_gain(sound_id, volume)


def _cache_wave(sound_id, volume, directory):
    value = wav_bytes(sound_id, volume)
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(value).hexdigest()[:16]
    target = directory / f"{sound_id}-{volume:03d}-{digest}.wav"
    if target.is_symlink():
        raise OSError("Alert cache file must not be a symbolic link")
    if target.is_file() and target.stat().st_size == len(value) and target.read_bytes() == value:
        return target
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".alert-", suffix=".tmp", dir=directory,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target


def play(sound_id, volume, cache_directory):
    """Submit one async local cue; False means intentional volume-zero silence.

    Playback/cache failures raise their original exception. Acceptance by Windows
    does not establish that a listener heard the cue. Other applications' volume
    settings and Windows mixer settings are never changed.
    """
    sound_id, volume = validate_sound_settings(sound_id, volume)
    if volume == 0:
        return False
    import winsound
    path = _cache_wave(sound_id, volume, cache_directory)
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    return True
