import unittest

from python.underwater.extract import sort_meshes_by_world_x


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


if __name__ == "__main__":
    unittest.main()
