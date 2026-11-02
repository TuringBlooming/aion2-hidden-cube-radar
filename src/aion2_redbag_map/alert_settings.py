"""Small local alert preferences, independent of the observation archive."""
import json
from pathlib import Path

from .audio import DEFAULT_SOUND, DEFAULT_VOLUME, validate_sound_settings
from .history_store import atomic_write

DEFAULTS = {"sound_profile": DEFAULT_SOUND, "sound_volume": DEFAULT_VOLUME,
            "overlay_on_alert": False}


def validate(changes):
    if not isinstance(changes, dict) or not changes or set(changes) - DEFAULTS.keys():
        raise ValueError("提醒设置无效")
    if "sound_profile" in changes:
        validate_sound_settings(changes["sound_profile"], DEFAULT_VOLUME)
    if "sound_volume" in changes:
        validate_sound_settings(DEFAULT_SOUND, changes["sound_volume"])
    if "overlay_on_alert" in changes and type(changes["overlay_on_alert"]) is not bool:
        raise ValueError("悬浮窗选项无效")
    return dict(changes)


def load(directory, fallback=None):
    result = dict(DEFAULTS)
    sources = [fallback or {}]
    try:
        sources.append(json.loads((Path(directory) / "alert-settings.json").read_text("utf-8")))
    except (OSError, ValueError):
        pass
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in DEFAULTS:
            if key in source:
                try:
                    result.update(validate({key: source[key]}))
                except ValueError:
                    pass
    return result


def save(directory, settings):
    values = dict(DEFAULTS, **validate(settings))
    atomic_write(Path(directory) / "alert-settings.json",
                 (json.dumps(values, ensure_ascii=False) + "\n").encode("utf-8"))
    return values
