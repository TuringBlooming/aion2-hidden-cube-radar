from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from aion2_redbag_map.map_markers import MARKER_COLORS, marker_image


class MapMarkerTests(unittest.TestCase):
    def test_native_sizes_have_transparent_corners_and_visible_centres(self):
        # Includes dense candidates, 100/125/150% map markers and large UI scale.
        for kind in MARKER_COLORS:
            for size in (8, 10, 12, 22, 28, 33, 44, 96):
                for selected in (False, True):
                    with self.subTest(kind=kind, size=size, selected=selected):
                        image = marker_image(kind, size, selected)
                        self.assertEqual(image.mode, "RGBA")
                        self.assertEqual(image.size, (size, size))
                        alpha = image.getchannel("A")
                        self.assertIsNotNone(alpha.getbbox())
                        self.assertGreater(alpha.getextrema()[1], 200)
                        for corner in ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1)):
                            self.assertEqual(alpha.getpixel(corner), 0)

    def test_statuses_and_selection_remain_distinct(self):
        normal = [marker_image(kind).tobytes() for kind in MARKER_COLORS]
        self.assertEqual(len(set(normal)), len(MARKER_COLORS))
        for kind in MARKER_COLORS:
            self.assertNotEqual(marker_image(kind, 22).tobytes(), marker_image(kind, 22, True).tobytes())

    def test_edges_have_partial_coverage_without_blurring_solid_fill(self):
        for kind in ("seen", "used", "soon", "player"):
            alpha = marker_image(kind, 28).getchannel("A")
            counts = alpha.histogram()
            self.assertGreater(sum(counts[1:255]), 0)
            self.assertGreater(counts[255], 50)

    def test_independent_images_and_invalid_arguments(self):
        image = marker_image("seen")
        expected = image.tobytes()
        image.paste((0, 0, 0, 0), (0, 0, 22, 22))
        self.assertEqual(marker_image("seen").tobytes(), expected)
        with self.assertRaises(ValueError):
            marker_image("invalid")
        for size in (7, 97, True, 12.5, "22"):
            with self.assertRaises(ValueError):
                marker_image("seen", size)


if __name__ == "__main__":
    unittest.main()
