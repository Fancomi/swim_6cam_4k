"""water_entry 标定叠图：线段必须落在带米标的顶点上。

水面的横向/纵向线与纵向网格的横向/纵向线都「画在点上」——每条 meter 线由该
meter 顶点之间实际存在的网格边组成。这里验证两条不变量：

  1. mesh.json 信息完整：两个网格都存在，带米标顶点占绝大多数（纵向网格最上/
     最下行、最右列是刻意跳过不标的，见 meters.py 的规则）。
  2. 每个带米标顶点都落在同 meter 的某条网格边附近（≤3px）——即画线会覆盖它。
     对水面纵向线（跳过最右列）、纵向网格横向线（0.0~1.5m）、纵向网格纵向线
     （全部）逐一断言。
"""
import json
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
SIZE = (1280, 720)


def load_mesh(line, cam, kind):
    path = ROOT / "outputs" / line / "overlay" / cam / "mesh.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    return next(m for m in document["meshes"] if m["kind"] == kind)


def meter_edges(mesh, axis, value):
    out = []
    for triangle in mesh["triangles"]:
        for i in range(3):
            a, b = triangle[i], triangle[(i + 1) % 3]
            ma, mb = a.get("meter"), b.get("meter")
            if (ma and mb and axis in ma and axis in mb
                    and abs(ma[axis] - value) < 1e-9):
                out.append((a["uv"], b["uv"]))
    return out


def dist_to_edges(uv, edges, tol=3.0):
    x = uv[0] * (SIZE[0] - 1)
    y = (1 - uv[1]) * (SIZE[1] - 1)
    for (a, b) in edges:
        x1, y1 = a[0] * (SIZE[0] - 1), (1 - a[1]) * (SIZE[1] - 1)
        x2, y2 = b[0] * (SIZE[0] - 1), (1 - b[1]) * (SIZE[1] - 1)
        dx, dy = x2 - x1, y2 - y1
        if dx == dy == 0:
            d = float(np.hypot(x - x1, y - y1))
        else:
            t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)))
            d = float(np.hypot(x - (x1 + t * dx), y - (y1 + t * dy)))
        if d <= tol:
            return True
    return False


def coverage(mesh, axis, predicate):
    """(covered, total) 满足 predicate 的 meter 顶点，逐个检查是否被同 meter 边覆盖。"""
    values = {}
    for triangle in mesh["triangles"]:
        for vertex in triangle:
            meter = vertex.get("meter")
            if meter and axis in meter:
                values.setdefault(meter[axis], []).append(vertex["uv"])
    covered = total = 0
    for value, uvs in values.items():
        if not predicate(value):
            continue
        edges = meter_edges(mesh, axis, value)
        for uv in uvs:
            total += 1
            covered += dist_to_edges(uv, edges)
    return covered, total


class OverlayCoverageTest(unittest.TestCase):
    def test_mesh_documents_are_complete(self):
        surface = load_mesh("water_entry","water_entry_b","surface")
        vertical = load_mesh("water_entry","water_entry_a","vertical")
        self.assertEqual(surface["kind"], "surface")
        self.assertEqual(vertical["kind"], "vertical")
        metered = lambda m: sum(1 for t in m["triangles"] for v in t if "meter" in v)
        total = lambda m: sum(len(t) for t in m["triangles"])
        self.assertGreater(metered(surface) / total(surface), 0.99)
        self.assertGreater(metered(vertical) / total(vertical), 0.99)

    def test_surface_lateral_lines_cover_their_vertices(self):
        """(1) 水面横向线（y 带）全部顶点在线上。"""
        surface = load_mesh("water_entry","water_entry_b","surface")
        covered, total = coverage(surface, "y", lambda m: True)
        self.assertEqual(covered, total)

    def test_surface_lane_lines_skip_the_rightmost_column(self):
        """(2) 水面纵向线：非最右列的全部顶点在线上。"""
        surface = load_mesh("water_entry","water_entry_b","surface")
        xm = set()
        for triangle in surface["triangles"]:
            for vertex in triangle:
                meter = vertex.get("meter")
                if meter and "x" in meter:
                    xm.add(meter["x"])
        xm = sorted(xm)
        skip = xm[0]                      # 最右列 = 右水线（0.5m）
        covered, total = coverage(surface, "x", lambda m: abs(m - skip) > 1e-9)
        self.assertEqual(covered, total)
        self.assertGreater(total, 0)

    def test_vertical_depth_lines_cover_up_to_1_5m(self):
        """(3) 纵向网格横向线 0.0~1.5m 全部顶点在线上。"""
        vertical = load_mesh("water_entry","water_entry_a","vertical")
        covered, total = coverage(vertical, "y", lambda m: m <= 1.5)
        self.assertEqual(covered, total)
        self.assertGreater(total, 0)

    def test_vertical_distance_lines_cover_every_column(self):
        """(4) 纵向网格纵向线全部顶点在线上。"""
        vertical = load_mesh("water_entry","water_entry_a","vertical")
        covered, total = coverage(vertical, "x", lambda m: True)
        self.assertEqual(covered, total)
        self.assertGreater(total, 0)


if __name__ == "__main__":
    unittest.main()
