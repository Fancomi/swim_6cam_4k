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
