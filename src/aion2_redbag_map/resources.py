"""Optional local map artwork. Never downloads or executes an asset pack."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re

from .history_store import atomic_write

ROOT = Path(__file__).resolve().parent
IMAGE_NAME = re.compile(r"(?:\d+\.webp|\d+/\d+_\d+\.webp)\Z")


def validate_maps(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 200:
        raise ValueError("地图清单格式无效")
    ids = set()
    for row in value:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0 or row["id"] in ids:
            raise ValueError("地图编号无效或重复")
        ids.add(row["id"])
        if not all(type(row.get(key)) in (int, float) and math.isfinite(row[key]) and abs(row[key]) <= 1e8
                   for key in ("x", "y", "w", "h")) or min(row["w"], row["h"]) <= 0:
            raise ValueError("地图坐标范围无效")
        if "tiles" in row and (not isinstance(row["tiles"], list) or len(row["tiles"]) != 2
                               or not all(type(n) is int and 1 <= n <= 32 for n in row["tiles"])):
            raise ValueError("地图分块数量无效")
        for key in ("name", "english_name"):
            if key in row and (not isinstance(row[key], str) or len(row[key]) > 120):
                raise ValueError("地图名称无效")
    return value


def load_maps(data_dir=None):
    custom = Path(data_dir) / "assets/maps.json" if data_dir else None
    if custom and custom.is_file():
        try:
            return validate_maps(json.loads(custom.read_text("utf-8")))
        except (OSError, ValueError, TypeError):
            pass
    return validate_maps(json.loads((ROOT / "maps.json").read_text("utf-8")))


def image_path(data_dir, relative):
    if not IMAGE_NAME.fullmatch(relative):
        return None
    folder = (Path(data_dir) / "assets/maps").resolve()
    path = folder / relative
    if path.resolve().is_relative_to(folder) and path.is_file():
        return path
    return None


def import_assets(source_dir, data_dir):
    """Import only validated WebP images and map geometry from a user-chosen folder."""
    from PIL import Image

    source = Path(source_dir).resolve()
    manifest = source / "maps.json"
    images = source / "maps"
    if not images.is_dir():
        images = source / "static/maps"
    if not manifest.is_file() or manifest.is_symlink() or not images.is_dir() or images.is_symlink():
        raise ValueError("请选择包含 maps.json 和 maps（或 static/maps）文件夹的资源目录")
    if manifest.stat().st_size > 256_000:
        raise ValueError("地图清单过大")
    maps = validate_maps(json.loads(manifest.read_text("utf-8")))
    raw_images = []
    total = 0
    for path in sorted(images.rglob("*.webp")):
        relative = path.relative_to(images).as_posix()
        if path.is_symlink() or not path.resolve().is_relative_to(source) or not IMAGE_NAME.fullmatch(relative):
            raise ValueError("资源含无效图片路径")
        total += path.stat().st_size
        if path.stat().st_size > 32 * 1024 * 1024 or total > 256 * 1024 * 1024 or len(raw_images) >= 4096:
            raise ValueError("地图资源过大")
        with Image.open(path) as im:
            if im.format != "WEBP" or im.width > 16384 or im.height > 16384:
                raise ValueError("仅支持有效的 WebP 地图图片")
            im.verify()
        raw_images.append((relative, path.read_bytes()))
    if not raw_images:
        raise ValueError("资源目录没有地图图片")
    # Validation completes before updating the local resource store. No source files are removed.
    destination = Path(data_dir) / "assets"
    for relative, raw in raw_images:
        atomic_write(destination / "maps" / relative, raw)
    atomic_write(destination / "maps.json", json.dumps(maps, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"maps": len(maps), "images": len(raw_images), "points": 0}
