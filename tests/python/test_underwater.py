import unittest
from pathlib import Path

from python.underwater.extract import sort_meshes_by_world_x

try:
    import fbx  # noqa: F401
    HAS_FBX = True
except Exception:
    HAS_FBX = False

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_01D = PROJECT_ROOT / "inputs" / "models" / "01d.fbx"
TEXDIR_01D = PROJECT_ROOT / "inputs" / "models" / "01d.fbm"


def _mesh(node, x0):
    # single triangle whose min pos[0] is x0
    tri = [
        {"pos": [x0, 0.0], "uv": [0.0, 0.0]},
        {"pos": [x0 + 1.0, 0.0], "uv": [1.0, 0.0]},
        {"pos": [x0, 1.0], "uv": [0.0, 1.0]},
    ]
    return {"node": node, "texture_basename": f"{node}.png", "triangles": [tri]}


class SortMeshesTest(unittest.TestCase):
    def test_orders_left_to_right_by_world_x(self):
        meshes = [_mesh("right", 5.0), _mesh("left", -2.0), _mesh("mid", 1.0)]
        ordered = sort_meshes_by_world_x(meshes)
        self.assertEqual([m["node"] for m in ordered], ["left", "mid", "right"])

    def test_does_not_mutate_input(self):
        meshes = [_mesh("right", 5.0), _mesh("left", -2.0)]
        sort_meshes_by_world_x(meshes)
        self.assertEqual([m["node"] for m in meshes], ["right", "left"])

    def test_empty_triangles_sort_last(self):
        empty = {"node": "empty", "texture_basename": "e.png", "triangles": []}
        meshes = [empty, _mesh("left", -2.0)]
        ordered = sort_meshes_by_world_x(meshes)
        self.assertEqual([m["node"] for m in ordered], ["left", "empty"])


class ExtractIntegrationTest(unittest.TestCase):
    @unittest.skipUnless(HAS_FBX, "FBX SDK not available")
    @unittest.skipUnless(MODEL_01D.is_file(), "01d.fbx not present")
    def test_extracts_two_ordered_meshes(self):
        import tempfile
        from python.underwater.extract import extract_to_json

        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "01d_mesh.json"
            meshes = extract_to_json(MODEL_01D, dst, TEXDIR_01D)

            self.assertTrue(dst.is_file())
            self.assertEqual(len(meshes), 2)
            # ordered left-to-right by world X: Box001 (min x ~ -0.57) before pPlane1 (~ -0.43)
            self.assertEqual(
                [m["node"] for m in meshes], ["Box001", "pPlane1"]
            )
            self.assertEqual(
                [m["texture_basename"] for m in meshes],
                ["underA2-grid.png", "underA1-grid.png"],
            )
            self.assertEqual([m["uvset"] for m in meshes], ["UVChannel_1", "map1"])


if __name__ == "__main__":
    unittest.main()


class RenderStillTest(unittest.TestCase):
    def test_resolve_ppm_targets_width(self):
        from python.underwater.render import resolve_ppm
        # world X span 2.0 -> ppm ~ 320 for 640 target
        self.assertAlmostEqual(resolve_ppm(-1.0, 1.0, 640), 320.0, places=3)

    def test_resolve_ppm_degenerate_span_falls_back(self):
        from python.underwater.render import resolve_ppm
        self.assertEqual(resolve_ppm(0.0, 0.0, 640), 100.0)

    def test_render_writes_still_and_grid(self):
        import json
        import tempfile
        import cv2
        import numpy as np
        from python.underwater.render import render_stills

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # one unit-square mesh mapped to a full texture
            tri_a = [
                {"pos": [0.0, 0.0], "uv": [0.0, 0.0]},
                {"pos": [1.0, 0.0], "uv": [1.0, 0.0]},
                {"pos": [1.0, 1.0], "uv": [1.0, 1.0]},
            ]
            tri_b = [
                {"pos": [0.0, 0.0], "uv": [0.0, 0.0]},
                {"pos": [1.0, 1.0], "uv": [1.0, 1.0]},
                {"pos": [0.0, 1.0], "uv": [0.0, 1.0]},
            ]
            data = {"source": "x", "meshes": [
                {"node": "p", "texture_basename": "t.png", "uvset": "map1",
                 "const_axis": 2, "kept_axes": [0, 1], "spans": [1, 1, 0],
                 "triangles": [tri_a, tri_b]},
            ]}
            data_path = td / "mesh.json"
            data_path.write_text(json.dumps(data))
            tex = np.full((16, 16, 3), 200, np.uint8)
            cv2.imwrite(str(td / "t.png"), tex)

            still = td / "out_stitch.png"
            grid = td / "out_grid.png"
            out_w, out_h = render_stills(
                data_path, td, still, grid, ppm=None,
                unit_scale=1.0, neg_v=False, target_width=64,
            )
            self.assertTrue(still.is_file())
            self.assertTrue(grid.is_file())
            img = cv2.imread(str(still))
            self.assertEqual(img.shape[1], out_w)
            self.assertEqual(img.shape[0], out_h)
            self.assertGreater(int(img.max()), 0)  # not all black

    def test_render_default_orientation_upright(self):
        import json
        import tempfile
        import cv2
        import numpy as np
        from python.underwater.render import render_stills

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tri_a = [
                {"pos": [0.0, 0.0], "uv": [0.0, 0.0]},
                {"pos": [1.0, 0.0], "uv": [1.0, 0.0]},
                {"pos": [1.0, 1.0], "uv": [1.0, 1.0]},
            ]
            tri_b = [
                {"pos": [0.0, 0.0], "uv": [0.0, 0.0]},
                {"pos": [1.0, 1.0], "uv": [1.0, 1.0]},
                {"pos": [0.0, 1.0], "uv": [0.0, 1.0]},
            ]
            data = {"source": "x", "meshes": [
                {"node": "p", "texture_basename": "t.png", "uvset": "map1",
                 "const_axis": 2, "kept_axes": [0, 1], "spans": [1, 1, 0],
                 "triangles": [tri_a, tri_b]},
            ]}
            data_path = td / "mesh.json"
            data_path.write_text(json.dumps(data))
            tex = np.full((16, 16, 3), 200, np.uint8)
            cv2.imwrite(str(td / "t.png"), tex)

            still = td / "out_stitch.png"
            grid = td / "out_grid.png"
            render_stills(data_path, td, still, grid, ppm=None, target_width=64)
            self.assertTrue(still.is_file())
            self.assertTrue(grid.is_file())
            img = cv2.imread(str(still))
            self.assertGreater(int(img.max()), 0)

    def test_crop_bottom_row_rescales_to_source_height(self):
        import numpy as np
        from python.underwater.render import crop_bottom_and_scale

        image = np.zeros((100, 200, 3), np.uint8)
        image[:80] = (10, 20, 30)
        image[80:] = (200, 210, 220)

        result = crop_bottom_and_scale(image, crop_px=20, target_height=100)

        self.assertEqual(result.shape, (100, 250, 3))
        self.assertLess(int(result[..., 0].max()), 100)

    def test_full_res_crop_bottom_restores_source_height_and_scales_width(self):
        import json
        import tempfile
        import cv2
        import numpy as np
        from python.underwater.render import render_stills

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            tri_a = [
                {"pos": [0.0, 0.0], "uv": [0.0, 0.0]},
                {"pos": [1.0, 0.0], "uv": [1.0, 0.0]},
                {"pos": [1.0, 1.0], "uv": [1.0, 1.0]},
            ]
            tri_b = [
                {"pos": [0.0, 0.0], "uv": [0.0, 0.0]},
                {"pos": [1.0, 1.0], "uv": [1.0, 1.0]},
                {"pos": [0.0, 1.0], "uv": [0.0, 1.0]},
            ]
            data = {"source": "x", "meshes": [
                {"node": "p", "texture_basename": "t.png", "uvset": "map1",
                 "const_axis": 2, "kept_axes": [0, 1], "spans": [1, 1, 0],
                 "triangles": [tri_a, tri_b]},
            ]}
            data_path = td / "mesh.json"
            data_path.write_text(json.dumps(data))
            cv2.imwrite(str(td / "t.png"), np.full((16, 32, 3), 200, np.uint8))
            still = td / "out.png"

            out_w, out_h = render_stills(
                data_path, td, still, None, full_res=True,
                margin=0, crop_bottom_px=4,
            )

            img = cv2.imread(str(still))
            self.assertEqual((out_w, out_h), (21, 16))
            self.assertEqual(img.shape[:2], (16, 21))

    def test_render_rejects_json_without_meshes(self):
        import json
        import tempfile
        from python.underwater.render import render_stills

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            bad_path = td / "bad.json"
            bad_path.write_text(json.dumps({"source": "x"}))
            still = td / "out_stitch.png"
            grid = td / "out_grid.png"
            with self.assertRaises(SystemExit):
                render_stills(bad_path, td, still, grid)
