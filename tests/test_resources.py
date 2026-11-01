import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.resources import ROOT, image_path, import_assets, load_maps, validate_maps


class ResourceImportTests(unittest.TestCase):
    def test_only_explicit_images_and_geometry_are_imported(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "chosen", root / "data"
            (source / "static/maps/1110").mkdir(parents=True)
            rows = [{"id": 1110, "name": "Example", "x": -100, "y": -200, "w": 300, "h": 400, "tiles": [1, 1]}]
            (source / "maps.json").write_text(json.dumps(rows), "utf-8")
            Image.new("RGB", (32, 32), "white").save(source / "static/maps/1110.webp")
            Image.new("RGB", (32, 32), "blue").save(source / "static/maps/1110/0_0.webp")
            (source / "history.json").write_text('{"private": true}', "utf-8")
            (source / "static/maps/start.cmd").write_text("never execute", "utf-8")
            result = import_assets(source, target)
            self.assertEqual(result, {"maps": 1, "images": 2, "points": 0})
            self.assertEqual(load_maps(target), rows)
            self.assertTrue(image_path(target, "1110/0_0.webp").is_file())
            self.assertIsNone(image_path(target, "../history.json"))
            self.assertIsNone(image_path(target, "1110/../../history.json"))
            self.assertFalse((target / "history.json").exists())
            self.assertFalse(any(target.rglob("*.cmd")))

    def test_invalid_pack_does_not_partially_overwrite_existing_assets(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, target = root / "chosen", root / "data"
            (source / "maps").mkdir(parents=True)
            (source / "maps.json").write_text(json.dumps([{"id": 1110, "x": 0, "y": 0, "w": 100, "h": 100}]), "utf-8")
            Image.new("RGB", (16, 16)).save(source / "maps/1110.webp")
            (source / "maps/9999.webp").write_bytes(b"not an image")
            (target / "assets").mkdir(parents=True)
            sentinel = target / "assets/maps.json"
            sentinel.write_bytes(b"existing manifest")
            with self.assertRaises(OSError):
                import_assets(source, target)
            self.assertEqual(sentinel.read_bytes(), b"existing manifest")
            self.assertFalse((target / "assets/maps").exists())

    def test_default_calibrated_maps_and_invalid_geometry(self):
        maps = load_maps()
        self.assertEqual(len(maps), 18)
        altgard = next(row for row in maps if row["id"] == 1110)
        self.assertEqual([altgard[key] for key in ("x", "y", "w", "h")], [-408000, -408000, 816000, 816000])
        self.assertEqual(altgard["tiles"], [8, 8])
        self.assertTrue(all("bounds_kind" not in row for row in maps))
        for value in ([], [{"id": 1, "x": float("nan"), "y": 0, "w": 100, "h": 100}],
                      [{"id": 1, "x": 0, "y": 0, "w": -1, "h": 100}]):
            with self.assertRaises(ValueError):
                validate_maps(value)

    def test_all_bundled_overviews_tiles_and_source_checksums_are_preserved(self):
        manifest = json.loads((ROOT / "map-source.json").read_text("utf-8"))
        self.assertEqual(manifest["source"], "A2Helper")
        self.assertEqual(manifest["release_version"], "0.1.29")
        folder = ROOT / "static/maps"
        files = {path.relative_to(folder).as_posix(): path for path in folder.rglob("*") if path.is_file()}
        self.assertEqual(len(files), 339)
        self.assertEqual(sum("/" not in name for name in files), 18)
        self.assertEqual(sum("/" in name for name in files), 321)
        self.assertEqual(set(files), set(manifest["map_assets"]))
        for relative, path in files.items():
            self.assertEqual(path.stat().st_size, manifest["map_assets"][relative]["bytes"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), manifest["map_assets"][relative]["sha256"])
        for row in load_maps():
            self.assertIn(f"{row['id']}.webp", files)
            if "tiles" in row:
                for x in range(row["tiles"][0]):
                    for y in range(row["tiles"][1]):
                        self.assertIn(f"{row['id']}/{x}_{y}.webp", files)


if __name__ == "__main__":
    unittest.main()
