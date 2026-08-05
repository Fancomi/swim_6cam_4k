"""Tests for normalized FBX UV mesh overlays."""
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from python.fbx_overlay.render import OverlayError, draw_mesh, draw_meshes, uv_to_pixel


ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "inputs/water_entry/background.jpg"
MESHES = (
    (ROOT / "inputs/water_entry/models/006.fbx", "Plane004", 140),
    (ROOT / "inputs/water_entry/models/005.fbx", "Plane005", 60),
)


def _triangle():
    return {
        "triangles": [[
            {"uv": [0.0, 0.0]},
            {"uv": [1.0, 0.0]},
            {"uv": [0.0, 1.0]},
        ]]
    }


class UvMappingTest(unittest.TestCase):
    def test_bottom_and_top_origins(self):
        self.assertEqual(uv_to_pixel([0.0, 0.0], (10, 20), "bottom").tolist(),
                         [0, 9])
        self.assertEqual(uv_to_pixel([0.0, 0.0], (10, 20), "top").tolist(),
                         [0, 0])
        self.assertEqual(uv_to_pixel([1.0, 1.0], (10, 20), "bottom").tolist(),
                         [19, 0])

    def test_outside_uv_is_not_clipped(self):
        self.assertEqual(uv_to_pixel([-0.1, 1.1], (10, 20)).tolist(),
                         [-2, -1])

    def test_invalid_options_raise(self):
        with self.assertRaises(OverlayError):
            uv_to_pixel([0.0, 0.0], (10, 20), "middle")
        with self.assertRaises(OverlayError):
            draw_mesh(np.zeros((10, 20), dtype=np.uint8), _triangle(), (0, 255, 0))
        with self.assertRaises(OverlayError):
            draw_mesh(np.zeros((10, 20, 3), dtype=np.uint8), _triangle(),
                      (0, 255, 0), fill_alpha=1.1)


class RenderingTest(unittest.TestCase):
    def test_draw_does_not_mutate_input(self):
        image = np.zeros((32, 48, 3), dtype=np.uint8)
        original = image.copy()
        result = draw_mesh(image, _triangle(), (0, 255, 0), thickness=2)
        np.testing.assert_array_equal(image, original)
        self.assertGreater(np.count_nonzero(result), 0)

    def test_two_meshes_use_distinct_colors(self):
        image = np.zeros((32, 48, 3), dtype=np.uint8)
        result = draw_meshes(
            image,
            [_triangle(), {"triangles": [[
                {"uv": [0.2, 0.2]},
                {"uv": [0.8, 0.2]},
                {"uv": [0.2, 0.8]},
            ]]}],
            thickness=1,
        )
        # LINE_AA blends edge pixels, so assert the two green-channel bands
        # rather than requiring an exact interior pixel for a one-pixel line.
        self.assertGreater(np.count_nonzero(result[:, :, 1] >= 220), 0)
        self.assertGreater(
            np.count_nonzero((result[:, :, 1] >= 90) &
                             (result[:, :, 1] <= 180)), 0)


@unittest.skipUnless(
    all(path.is_file() for path, _node, _count in MESHES) and IMAGE.is_file(),
    "local FBX and xlj image assets are not available",
)
class RealAssetTest(unittest.TestCase):
    def test_known_nodes_and_cli_output(self):
        from python.fbx_overlay.__main__ import _load_mesh, main

        loaded = []
        for path, node_name, triangle_count in MESHES:
            mesh = _load_mesh(path, node_name)
            loaded.append(mesh)
            self.assertEqual(mesh["node"], node_name)
            self.assertEqual(len(mesh["triangles"]), triangle_count)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "overlay.png"
            self.assertEqual(main(["--output", str(output)]), 0)
            rendered = cv2.imread(str(output), cv2.IMREAD_COLOR)
            self.assertIsNotNone(rendered)
            self.assertEqual(rendered.shape, (720, 1280, 3))
            base = cv2.imread(str(IMAGE), cv2.IMREAD_COLOR)
            self.assertGreater(np.count_nonzero(rendered != base), 0)
            self.assertGreater(np.count_nonzero(np.all(rendered == [0, 255, 255], axis=2)), 0)
            self.assertGreater(np.count_nonzero(np.all(rendered == [0, 128, 255], axis=2)), 0)
