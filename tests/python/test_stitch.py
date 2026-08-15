"""Stitch line tests: geometry, shaping, alignment, and the step dispatcher.

The three lines share one code path, so most tests here assert that a per-line
difference really is a profile field and not a branch.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from python.stitch import compose as C
from python.stitch import profiles as P
from python.stitch.extract import select_planes, sort_by_world_x

try:
    import fbx  # noqa: F401
    HAS_FBX = True
except Exception:
    HAS_FBX = False

ROOT = Path(__file__).resolve().parents[2]
POOL_FBX = ROOT / "inputs" / "pool" / "models" / "pool.fbx"
OVERHEAD_FBX = ROOT / "inputs" / "overhead" / "models" / "002.fbx"


def _quad(x0, x1, y0, y1, uv=(0.0, 1.0)):
    """Two triangles covering [x0,x1]x[y0,y1], UV spanning `uv` on both axes."""
    u0, u1 = uv
    corners = {(x0, y0): (u0, u0), (x1, y0): (u1, u0),
               (x1, y1): (u1, u1), (x0, y1): (u0, u1)}

    def vertex(x, y):
        u, v = corners[(x, y)]
        return {"pos": [x, y], "uv": [u, v]}

    return [[vertex(x0, y0), vertex(x1, y0), vertex(x1, y1)],
            [vertex(x0, y0), vertex(x1, y1), vertex(x0, y1)]]


def _mesh(node, x0, x1=None, y0=0.0, y1=1.0, tex=None, uv=(0.0, 1.0)):
    x1 = x0 + 1.0 if x1 is None else x1
    return {"node": node, "texture_basename": tex if tex is not None else f"{node}.png",
            "uvset": "map1", "const_axis": 2, "kept_axes": [0, 1],
            "spans": [x1 - x0, y1 - y0, 0.0],
            "triangles": _quad(x0, x1, y0, y1, uv)}


def _mesh_json(directory, meshes):
    path = Path(directory) / "mesh.json"
    path.write_text(json.dumps({"source": "fixture", "meshes": meshes}))
    return path


class MeshOrderTest(unittest.TestCase):
    def test_orders_left_to_right_by_world_x(self):
        meshes = [_mesh("right", 5.0), _mesh("left", -2.0), _mesh("mid", 1.0)]
        self.assertEqual([m["node"] for m in sort_by_world_x(meshes)],
                         ["left", "mid", "right"])

    def test_does_not_mutate_its_input(self):
        meshes = [_mesh("b", 5.0), _mesh("a", -2.0)]
        sort_by_world_x(meshes)
        self.assertEqual([m["node"] for m in meshes], ["b", "a"])

    def test_keeps_one_full_height_plane_per_texture(self):
        # A real plane is tall and inside the pool Y band; the clutter in all.fbx
        # is either untextured, short, or a duplicate with fewer triangles.
        plane = _mesh("plane", 0.0, 4.0, -11.0, -8.2, tex="cam.png")
        duplicate = dict(plane, node="copy", triangles=plane["triangles"][:1])
        strip = _mesh("strip", 0.0, 4.0, -0.2, 0.2, tex="cam.png")
        untextured = _mesh("frame", 0.0, 4.0, -11.0, -8.2, tex=None)
        untextured["texture_basename"] = None

        kept = select_planes([duplicate, strip, plane, untextured])
        self.assertEqual([m["node"] for m in kept], ["plane"])

    def test_rejects_a_model_outside_the_pool_band(self):
        # 002.fbx spans Y [20.47, 23.47]; the filter is for all.fbx only, which
        # is why the overhead line leaves planes_only off.
        overhead = _mesh("Plane001", -35.0, -27.0, 20.47, 23.47, tex="C06.jpg")
        self.assertEqual(select_planes([overhead]), [])


class CanvasTest(unittest.TestCase):
    def test_size_follows_world_span_and_density(self):
        canvas = C.Canvas([_mesh("a", 0.0, 2.0, 0.0, 1.0)], 100.0, margin=0)
        self.assertEqual((canvas.width, canvas.height), (201, 101))

    def test_margin_pads_both_sides(self):
        canvas = C.Canvas([_mesh("a", 0.0, 2.0, 0.0, 1.0)], 100.0, margin=2)
        self.assertEqual((canvas.width, canvas.height), (205, 105))

    def test_projection_puts_world_y_up_and_canvas_y_down(self):
        meshes = [_mesh("a", 0.0, 1.0, 0.0, 1.0)]
        canvas = C.Canvas(meshes, 10.0, margin=0)
        bottom_left, _bottom_right, top_right = canvas.project(
            meshes[0]["triangles"][0])
        self.assertAlmostEqual(bottom_left[0], 0.0)
        self.assertAlmostEqual(bottom_left[1], canvas.height - 1)
        self.assertAlmostEqual(top_right[1], 0.0)

    def test_adaptive_ppm_matches_source_height_when_given_one(self):
        self.assertAlmostEqual(
            C.adaptive_ppm([_mesh("a", 0.0, 8.0, 0.0, 2.0)], 720), 360.0)

    def test_adaptive_ppm_falls_back_to_a_target_width(self):
        self.assertAlmostEqual(
            C.adaptive_ppm([_mesh("a", -1.0, 1.0)], None, 640), 320.0)

    def test_adaptive_ppm_survives_a_degenerate_span(self):
        self.assertEqual(C.adaptive_ppm([_mesh("a", 0.0, 0.0, 0.0, 0.0)], None), 100.0)

    def test_to_metres_flips_y_only_when_asked(self):
        upright = [_mesh("a", 0.0, 1.0, 2.0, 4.0)]
        flipped = json.loads(json.dumps(upright))
        C.to_metres(upright, 1.0, False)
        C.to_metres(flipped, 1.0, True)
        self.assertEqual(upright[0]["triangles"][0][0]["pos"][1], 2.0)
        self.assertEqual(flipped[0]["triangles"][0][0]["pos"][1], -2.0)

    def test_to_metres_flips_x_only_when_asked(self):
        """neg_u 单独存在才能表达"只镜像 X"；与 neg_v 合起来就是 180° 旋转。"""
        plain = [_mesh("a", 1.0, 3.0, 2.0, 4.0)]
        mirrored = json.loads(json.dumps(plain))
        C.to_metres(plain, 1.0, False, False)
        C.to_metres(mirrored, 1.0, False, True)
        self.assertEqual(plain[0]["triangles"][0][0]["pos"][0], 1.0)
        self.assertEqual(mirrored[0]["triangles"][0][0]["pos"][0], -1.0)
        # X 翻了不该动 Y
        self.assertEqual(mirrored[0]["triangles"][0][0]["pos"][1], 2.0)

    def test_to_metres_both_mirrors_is_a_half_turn(self):
        """两个镜像一起开 = 绕原点转 180°：每个顶点都取相反数。"""
        plain = [_mesh("a", 1.0, 3.0, 2.0, 4.0)]
        turned = json.loads(json.dumps(plain))
        C.to_metres(plain, 1.0, False, False)
        C.to_metres(turned, 1.0, True, True)
        for tri_a, tri_b in zip(plain[0]["triangles"], turned[0]["triangles"]):
            for a, b in zip(tri_a, tri_b):
                self.assertEqual(b["pos"][0], -a["pos"][0])
                self.assertEqual(b["pos"][1], -a["pos"][1])


class BlendTest(unittest.TestCase):
    """clip and blend_px are the two knobs separating the pool's distance feather
    from the plane lines' vertical seam."""

    def _layers(self, clip, uv=(0.0, 1.0)):
        """Two overlapping planes at 64 px/m over a 32x32 source."""
        meshes = [_mesh("left", 0.0, 1.0, 0.0, 1.0, uv=uv),
                  _mesh("right", 0.8, 1.8, 0.0, 1.0, uv=uv)]
        canvas = C.Canvas(meshes, 64.0, margin=0)
        return canvas, [C.build_remap(m, canvas, (32, 32), clip=clip)
                        for m in meshes]

    def test_clip_removes_coverage_where_uv_leaves_the_image(self):
        _canvas, plain = self._layers(clip=False, uv=(-0.2, 1.2))
        _canvas, clipped = self._layers(clip=True, uv=(-0.2, 1.2))
        for loose, tight in zip(plain, clipped):
            self.assertGreater(int(loose[2].sum()), int(tight[2].sum()))

    def test_clip_keeps_everything_that_samples_inside(self):
        # UV 0..1 maps to source coordinate 0..tex_size, so the far edge lands one
        # past the last index and clipping always trims that hairline. A UV that
        # stays comfortably inside loses nothing, which is what makes clip safe to
        # leave on for the plane lines.
        _canvas, plain = self._layers(clip=False, uv=(0.05, 0.9))
        _canvas, clipped = self._layers(clip=True, uv=(0.05, 0.9))
        for loose, tight in zip(plain, clipped):
            self.assertTrue(np.array_equal(loose[2], tight[2]))

    def test_a_triangle_reaching_past_the_raster_is_clipped(self):
        """越出画布的三角形要裁掉，不能靠 margin 保证"永远不越界"。

        margin=0 的资产画布上，underwater2 的平面顶行实测落在 y=-1（浮点取整），
        而负的切片起点会从对侧边缘取，既不报错也画错地方——所以 build_remap 自己
        跟画布求交。这里把整块网格上移半米，让它必然越界。"""
        mesh = _mesh("high", 0.0, 1.0, 0.5, 1.5)
        canvas = C.Canvas([_mesh("base", 0.0, 1.0, 0.0, 1.0)], 64.0, margin=0)
        m1, m2, mask = C.build_remap(mesh, canvas, (32, 32))
        self.assertEqual(mask.shape, canvas.shape)
        # 下半部分（落在画布内的那半）有覆盖，且没有任何像素被画到对侧边缘
        self.assertGreater(int(mask.sum()), 0)
        self.assertGreater(int(mask[0].sum()), 0)          # 顶部被裁到 y=0
        self.assertEqual(int(mask[-1].sum()), 0)           # 底部本不该有内容

    def test_a_triangle_entirely_outside_paints_nothing(self):
        mesh = _mesh("away", 10.0, 11.0, 10.0, 11.0)
        canvas = C.Canvas([_mesh("base", 0.0, 1.0, 0.0, 1.0)], 64.0, margin=0)
        _m1, _m2, mask = C.build_remap(mesh, canvas, (32, 32))
        self.assertEqual(int(mask.sum()), 0)

    def test_weights_sum_to_one_wherever_anything_is_covered(self):
        canvas, layers = self._layers(clip=True)
        masks = [layer[2] for layer in layers]
        covered = np.zeros(canvas.shape, bool)
        for mask in masks:
            covered |= mask > 0
        for blend in (None, 0.0, 20.0):
            total = sum(C.blend_weights(masks, blend))
            np.testing.assert_allclose(total[covered], 1.0, atol=1e-5)
            self.assertEqual(float(total[~covered].max(initial=0.0)), 0.0)

    def test_a_hard_cut_gives_every_pixel_to_one_lane(self):
        _canvas, layers = self._layers(clip=True)
        weights = C.blend_weights([layer[2] for layer in layers], 0.0)
        self.assertEqual(int((np.stack(weights) > 0).sum(axis=0).max()), 1)

    def test_a_blend_band_shares_pixels_across_the_seam(self):
        _canvas, layers = self._layers(clip=True)
        weights = C.blend_weights([layer[2] for layer in layers], 20.0)
        self.assertTrue(((np.stack(weights) > 0).sum(axis=0) > 1).any())

    def test_the_seam_is_vertical(self):
        # Every row must hand over at the same column, which is what a
        # horizontal-depth blend guarantees and a 2-D distance transform does not.
        _canvas, layers = self._layers(clip=True)
        left, _right = C.blend_weights([layer[2] for layer in layers], 0.0)
        edges = {int(row[-1]) for row in
                 (np.flatnonzero(r > 0) for r in left) if len(row)}
        self.assertEqual(len(edges), 1)

    def test_feather_keeps_single_coverage_opaque(self):
        # An edge pixel only one lane reaches must not be dimmed, or the pool
        # composite darkens along its outer border.
        meshes = [_mesh("a", 0.0, 1.0), _mesh("b", 5.0, 6.0)]      # disjoint
        canvas = C.Canvas(meshes, 32.0, margin=0)
        masks = [C.build_remap(m, canvas, (8, 8))[2] for m in meshes]
        for weight, mask in zip(C.blend_weights(masks, None), masks):
            np.testing.assert_allclose(weight[mask > 0], 1.0, atol=1e-6)


class LaneSpanTest(unittest.TestCase):
    """视野区间图：每台相机一条 |-- --|，实线独占、虚线过渡，相邻交替高度。"""

    def _weights(self, spans, blend_px=0.0, ppm=32.0):
        meshes = [_mesh("m%d" % i, x0, x1) for i, (x0, x1) in enumerate(spans)]
        canvas = C.Canvas(meshes, ppm, margin=0)
        masks = [C.build_remap(m, canvas, (8, 8))[2] for m in meshes]
        return C.blend_weights(masks, blend_px), canvas

    def test_span_separates_the_view_from_what_it_owns(self):
        """四个数字分开两件事：能看到多少（端点）与其中多少是自己独占的（实线）。

        合成一个区间会把"我能看到这里"和"这里归我"混为一谈，而这正是这张图要
        回答的问题。"""
        weights, _canvas = self._weights([(0.0, 2.0), (1.0, 3.0)], blend_px=8.0)
        spans = C.lane_spans(weights)
        for weight, span in zip(weights, spans):
            cov0, own0, own1, cov1 = span
            covered = np.flatnonzero((weight > 0).any(axis=0))
            owned = np.flatnonzero((weight >= 0.99).any(axis=0))
            self.assertEqual((cov0, cov1), (int(covered[0]), int(covered[-1])))
            self.assertEqual((own0, own1), (int(owned[0]), int(owned[-1])))
            self.assertLessEqual(cov0, own0)
            self.assertLessEqual(own1, cov1)

    def test_the_middle_lane_is_dashed_on_both_sides(self):
        """夹在两块之间的那条，左右都该有过渡带；两端那两条各只有一侧。"""
        weights, _canvas = self._weights(
            [(0.0, 2.0), (1.5, 3.5), (3.0, 5.0)], blend_px=8.0)
        left, middle, right = C.lane_spans(weights)
        self.assertEqual(left[0], left[1])            # 最左：左侧无过渡
        self.assertLess(middle[0], middle[1])         # 中间：左有
        self.assertLess(middle[2], middle[3])         # 中间：右也有
        self.assertEqual(right[2], right[3])          # 最右：右侧无过渡

    def test_a_lane_with_no_solid_region_still_gets_a_span(self):
        """完全落在别人过渡带里的一块，实线长度收成 0 但仍要画出视野区间——
        少一条杠会被读成少一台相机。旧网格的 A10/A5 就是这样（重叠太宽）。"""
        weights = [np.full((4, 6), 0.5, np.float32), np.full((4, 6), 0.5, np.float32)]
        spans = C.lane_spans(weights)
        for span in spans:
            self.assertIsNotNone(span)
            self.assertEqual(span[1], span[2])        # 无独占区
            self.assertEqual((span[0], span[3]), (0, 5))

    def test_a_lane_owning_nothing_draws_all_dashed(self):
        """独占区为 0 时整条画成虚线，而不是只剩两个端点——虚线才表达
        "我看到的每一处都跟别人共享"。"""
        image = np.zeros((120, 300, 3), np.uint8)
        C.draw_spans(image, [(20, 150, 150, 280)], ["shared"], levels=1)
        middle = image[:, 60:240]
        columns = np.flatnonzero(middle.any(axis=(0, 2)))
        self.assertGreater(len(columns), 20)          # 中段有笔画
        self.assertLess(len(columns), middle.shape[1])  # 但不连续（是虚线）

    def test_ownership_bounds_are_the_outer_extent(self):
        """独占区可能被邻居切成两段（三块交汇处的偏置就会这样），
        取"最深列附近的连续段"会静默丢掉另一段，所以取外边界。"""
        weight = np.zeros((4, 10), np.float32)
        weight[:, 1] = 1.0                            # 左段
        weight[:, 5] = 0.5                            # 中间归邻居
        weight[:, 8] = 1.0                            # 右段
        weight[:, 0] = weight[:, 9] = 0.2             # 两端过渡
        span = C.lane_spans([weight])[0]
        self.assertEqual((span[1], span[2]), (1, 8))
        self.assertEqual((span[0], span[3]), (0, 9))

    def test_an_uncovered_lane_has_no_span(self):
        weights = [np.zeros((4, 6), np.float32), np.ones((4, 6), np.float32)]
        self.assertIsNone(C.lane_spans(weights)[0])
        self.assertIsNotNone(C.lane_spans(weights)[1])

    def test_neighbours_alternate_height(self):
        """相邻的杠必然重叠（重叠就是融合区），画在同一高度会让虚线端互相盖住，
        分不出谁是谁——所以交替两个高度。"""
        image = np.zeros((200, 400, 3), np.uint8)
        C.draw_spans(image, [(0, 40, 200, 240), (200, 240, 399, 399)],
                     ["a", "b"], levels=2)
        rows = [np.flatnonzero(image[:, x].any(axis=1)) for x in (120, 320)]
        self.assertTrue(len(rows[0]) and len(rows[1]))
        self.assertNotEqual(int(np.median(rows[0])), int(np.median(rows[1])))

    def test_every_lane_gets_the_same_text_size(self):
        """字号统一：宽度信息已经由杠表达，再按块宽缩字会让窄块的编号看不清，
        还会诱人把字号大小当成一种含义。

        只量文字所在的行（杠压在底部），否则量到的是杠本身的长度。"""
        def text_width(span):
            image = np.zeros((200, 400, 3), np.uint8)
            C.draw_spans(image, [span], ["underA10"], levels=1, band=(0.9, 0.9))
            text_rows = image[:int(200 * 0.9) - 10]
            painted = np.flatnonzero(text_rows.any(axis=(0, 2)))
            return int(painted[-1] - painted[0] + 1) if len(painted) else 0

        wide, narrow = text_width((0, 10, 390, 399)), text_width((180, 195, 205, 220))
        self.assertGreater(narrow, 0)
        self.assertAlmostEqual(wide, narrow, delta=4)

    def test_labels_and_caps_stay_inside_the_canvas(self):
        image = np.zeros((120, 300, 3), np.uint8)
        C.draw_spans(image, [(0, 0, 299, 299)], ["underA1"], levels=1)
        painted = np.flatnonzero(image.any(axis=(0, 2)))
        self.assertGreaterEqual(painted[0], 0)
        self.assertLess(painted[-1], image.shape[1])

    def test_a_missing_span_is_skipped_not_drawn_at_zero(self):
        image = np.zeros((120, 300, 3), np.uint8)
        C.draw_spans(image, [None, (100, 120, 180, 200)], ["gone", "here"])
        self.assertEqual(int(image[:, :50].sum()), 0)

    def test_grid_carries_no_labels(self):
        """grid 只管几何：十六套三角边已经很密，身份是另一张图的事。"""
        meshes = [_mesh("a", 0.0, 2.0), _mesh("b", 1.0, 3.0)]
        canvas = C.Canvas(meshes, 32.0, margin=0)
        base = np.zeros((canvas.height, canvas.width, 3), np.uint8)
        import inspect
        # Only an optional palette override is allowed — never a label/name
        # mechanism (identity is a separate question, answered by draw_spans).
        self.assertEqual(list(inspect.signature(C.draw_grid).parameters),
                         ["image", "meshes", "canvas", "colors"])
        self.assertIsNotNone(C.draw_grid(base, meshes, canvas))


class CropTest(unittest.TestCase):
    def test_bottom_dirty_rows_counts_the_ragged_tail(self):
        coverage = np.zeros((10, 5), np.uint8)
        coverage[:7] = 1
        coverage[7, :3] = 1
        coverage[8, :1] = 1                        # row 9 stays empty
        self.assertEqual(C.bottom_dirty_rows(coverage), 3)

    def test_bottom_dirty_rows_is_zero_on_a_clean_canvas(self):
        self.assertEqual(C.bottom_dirty_rows(np.ones((8, 5), np.uint8)), 0)

    def test_bottom_dirty_rows_measures_against_the_widest_row(self):
        # A constant margin leaves zero columns even in a full row, so the
        # reference is the per-canvas maximum and not the full width.
        coverage = np.zeros((6, 10), np.uint8)
        coverage[:5, 2:8] = 1
        coverage[5, 2:5] = 1
        self.assertEqual(C.bottom_dirty_rows(coverage), 1)

    def test_crop_and_scale_restores_the_target_height(self):
        image = np.zeros((100, 200, 3), np.uint8)
        image[:80] = (10, 20, 30)
        image[80:] = (200, 210, 220)
        result = C.crop_and_scale(image, crop_px=20, target_height=100)
        self.assertEqual(result.shape, (100, 250, 3))
        self.assertLess(int(result[..., 0].max()), 100)    # the bright tail is gone

    def test_crop_and_scale_rejects_removing_everything(self):
        with self.assertRaises(ValueError):
            C.crop_and_scale(np.zeros((10, 10, 3), np.uint8), 10, 10)


class LoadMeshesTest(unittest.TestCase):
    def test_missing_file_names_the_step_that_makes_it(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(P.StepError) as caught:
                C.load_meshes(Path(td) / "absent.json")
            self.assertIn("extract", str(caught.exception))

    def test_json_without_meshes_is_refused(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.json"
            path.write_text(json.dumps({"source": "x"}))
            with self.assertRaises(P.StepError):
                C.load_meshes(path)


class ProfileTest(unittest.TestCase):
    """A profile record is the single place a line's differences live."""

    def test_registry_holds_every_line(self):
        self.assertEqual(P.names(),
                         ["pool", "pool2", "underwater", "underwater2",
                          "overhead"])

    def test_pool2_mirrors_both_axes_to_land_on_pools_layout(self):
        """pool2 是同一批相机、同一批片段，只换了设计师手工重建的 FBX。

        新文件把泳池建成相对 pool.fbx 转了 180°，所以两个镜像都要开：只开一个
        会变成单轴翻转（对不上），都不开则整池转 180°。相机身份按每块 mesh 自己
        的贴图认（.fbm 文件名是从别处复用的，像素才是真身），不能按世界位置配。"""
        pool2 = P.get("pool2")
        self.assertEqual(pool2.camera_ids,
                         ("cam5", "cam6", "cam4", "cam1", "cam3", "cam2"))
        self.assertTrue(pool2.neg_u)              # 两个镜像合起来 = 180° 旋转
        self.assertTrue(pool2.neg_v)
        self.assertEqual(pool2.order, "declared")
        # 其余渲染口径与 pool 一致，两条线才可逐帧对比
        pool = P.get("pool")
        for field in ("ppm", "source_size", "blend_px", "clip_uv",
                      "still_margin", "sync", "clip_suffix"):
            self.assertEqual(getattr(pool2, field), getattr(pool, field), field)
        # 产物分开放，不覆盖旧线的产物
        self.assertNotEqual(pool2.out_dir, pool.out_dir)

    def test_only_pool2_needs_the_x_mirror(self):
        """neg_u 是这个文件的属性，不是全局默认——其余线一个都不该开。"""
        for name in ("pool", "underwater", "underwater2", "overhead"):
            self.assertFalse(P.get(name).neg_u, name)

    def test_pool2_lays_the_banks_out_opposite_to_pool(self):
        """两条线的 6 台相机集合相同，但排布相反——这正是不能按位置配的原因。"""
        pool, pool2 = P.get("pool"), P.get("pool2")
        self.assertEqual(set(pool2.camera_ids), set(pool.camera_ids))
        # 远排（声明序前三）：pool 是 cam3/cam2/cam1，pool2 是 cam5/cam6/cam4
        self.assertEqual(pool.camera_ids[:3], ("cam3", "cam2", "cam1"))
        self.assertEqual(pool2.camera_ids[:3], ("cam5", "cam6", "cam4"))

    def test_both_pool_lines_default_to_the_same_clips(self):
        """同一场录制：默认片段目录必须一致，否则"对比"比的是两批素材。"""
        self.assertEqual(P.default_video_dir(P.get("pool2")),
                         P.default_video_dir(P.get("pool")))
        self.assertIsNone(P.default_video_dir(P.get("underwater")))

    def test_pool_values_reproduce_the_shipped_asset(self):
        # The committed pool_4k.swasset was baked with exactly these; a drift
        # here silently changes the runtime geometry.
        pool = P.get("pool")
        self.assertEqual(pool.camera_ids,
                         ("cam3", "cam2", "cam1", "cam4", "cam5", "cam6"))
        self.assertEqual(pool.ppm, 100.0)
        self.assertIsNone(pool.blend_px)          # distance feather
        self.assertFalse(pool.clip_uv)
        self.assertTrue(pool.neg_v)
        self.assertEqual(pool.order, "declared")
        self.assertEqual(pool.still_margin, 0)
        self.assertFalse(pool.full_res)
        self.assertEqual(pool.crop_bottom, "none")
        self.assertEqual(pool.sync, "none")

    def test_underwater_values_match_the_shipped_pipeline(self):
        line = P.get("underwater")
        self.assertEqual(line.camera_ids,
                         tuple(f"underA{i}" for i in range(16, 0, -1)))
        self.assertEqual(line.clip_suffix, ".ts")
        self.assertEqual(line.ppm, 240.0)
        self.assertEqual(line.blend_px, 120.0)
        self.assertTrue(line.full_res)
        self.assertEqual(line.crop_bottom, "auto")
        self.assertTrue(line.clip_uv)
        self.assertFalse(line.neg_v)
        self.assertTrue(line.planes_only)
        self.assertEqual(line.sync, "manifest")
        self.assertEqual(line.source_size, (1280, 720))
        self.assertEqual(line.ref_tex, "snapshot")
        self.assertEqual(line.asset.name, "underwater.swasset")

    def test_underwater2_reuses_underwaters_shaping(self):
        """underwater2 是同 15 台相机（旧 16 台去了 A1）、同一批片段。

        渲染口径必须与 underwater 逐字段一致，否则"对比"比的是两套参数而不是两个
        网格。ppm 是绝对量（px/m），所以世界尺度变了也不用改它。唯一例外是
        camera_ids：8.14-02 版本把 underA1 从网格里去掉（只有 15 个 mesh），
        所以这行只有 15 台相机。"""
        line, old = P.get("underwater2"), P.get("underwater")
        for field in ("clip_suffix", "ppm", "blend_px", "clip_uv",
                      "full_res", "crop_bottom", "source_size", "sync",
                      "neg_v", "neg_u", "order", "still_margin"):
            self.assertEqual(getattr(line, field), getattr(old, field), field)
        # 相机数少了 A1：新网格是 15 节点 02..16，贴图是裸背景。
        self.assertEqual(line.camera_ids,
                         tuple(f"underA{i}" for i in range(16, 1, -1)))
        # 只挪顶点/换背景的改版不是新线：文件名带日期，但仍是同 15 个节点。
        self.assertEqual(line.fbx.name, "8.15.fbx")
        self.assertEqual(line.fbx.with_suffix(".fbm"), line.tex_dir)
        self.assertNotEqual(line.out_dir, old.out_dir)   # 不覆盖旧线产物

    def test_underwater2_must_not_filter_planes(self):
        """这个文件只有 15 个节点，且平面落在 select_planes 硬编码 band 之外。

        band 是 (-11.6, -8.0)，8.14-02 改版后这些平面是 (-10.09, -7.34)——开了
        过滤会把 15 块全丢掉并报 "no pool plane found"，和当初 overhead 一样的坑。"""
        self.assertFalse(P.get("underwater2").planes_only)
        self.assertTrue(P.get("underwater").planes_only)
        plane = _mesh("02", 59.95, 62.95, -10.09, -7.34,
                      tex="underA2_background.png")
        self.assertEqual(select_planes([plane]), [])

    def test_underwater2_reads_reference_textures_from_video(self):
        """underwater 走 snapshot，但快照现在多了一层日期目录，
        frames_for_camera(裸相机名) 什么都找不到，tex 会导出空目录。"""
        self.assertEqual(P.get("underwater2").ref_tex, "video")
        self.assertEqual(P.get("underwater").ref_tex, "snapshot")

    def test_underwater2_keeps_one_texture_directory(self):
        """underwater 把 .fbm 和 still 贴图分开是因为 .fbm 里的副本过期了；
        8.15.fbm 装的就是交付的裸背景本身，所以一个目录服务两个读者。"""
        line = P.get("underwater2")
        self.assertEqual(line.still_textures, line.tex_dir)

        line = P.get("overhead")
        self.assertEqual(line.camera_ids, ("overhead5", "overhead6"))
        self.assertEqual(line.ppm, 170.0)
        self.assertEqual(line.blend_px, 85.0)
        self.assertFalse(line.full_res)
        self.assertEqual(line.crop_bottom, "none")
        self.assertTrue(line.clip_uv)
        self.assertFalse(line.planes_only)
        self.assertEqual(line.sync, "manifest")
        self.assertEqual(line.source_size, (3840, 2160))
        self.assertEqual(line.ref_tex, "video")
        self.assertEqual(line.fbx.name, "002.fbx")

    def test_pool_keeps_declared_order_because_its_meshes_are_two_rows(self):
        # World-X order would interleave the banks and pair each camera with the
        # opposite one's plane; only the plane lines are a single row.
        self.assertEqual(P.get("pool").order, "declared")
        self.assertEqual(P.get("underwater").order, "world_x")
        self.assertEqual(P.get("underwater2").order, "world_x")
        self.assertEqual(P.get("overhead").order, "world_x")

    def test_unknown_name_lists_the_registered_ones(self):
        with self.assertRaises(SystemExit) as caught:
            P.get("nosuchline")
        message = str(caught.exception)
        self.assertIn("nosuchline", message)
        for name in P.names():
            self.assertIn(name, message)

    def test_a_profile_is_immutable(self):
        import dataclasses
        with self.assertRaises(dataclasses.FrozenInstanceError):
            P.get("overhead").ppm = 1.0

    def test_every_line_has_its_own_output_dir_asset_and_config(self):
        lines = [P.get(name) for name in P.names()]
        for attribute in ("out_dir", "asset", "metrics", "mesh_json"):
            values = [getattr(line, attribute) for line in lines]
            self.assertEqual(len(set(values)), len(values), attribute)
        configs = [line.config_path("metal") for line in lines]
        self.assertEqual(len(set(configs)), len(configs))

    def test_still_textures_default_to_the_model_directory(self):
        # Only underwater splits them: its canonical grids live in the dataset,
        # while the .fbm copies are stale.
        for name in ("pool", "pool2", "underwater2", "overhead"):
            line = P.get(name)
            self.assertEqual(line.still_textures, line.tex_dir)
        self.assertNotEqual(P.get("underwater").still_textures,
                            P.get("underwater").tex_dir)

    def test_only_pool_has_a_default_clip_directory(self):
        # The plane lines are per-sample directories chosen per run; pool's is a
        # machine-wide session.
        self.assertIsNotNone(P.default_video_dir(P.get("pool")))
        self.assertIsNone(P.default_video_dir(P.get("overhead")))
        self.assertIsNone(P.default_video_dir(P.get("underwater")))
        self.assertIsNone(P.default_video_dir(P.get("underwater2")))

    def test_clip_for_matches_the_suffix(self):
        overhead = P.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "swb_x_overhead5.ts").write_bytes(b"")
            (td / "swb_x_overhead5.mp4").write_bytes(b"")      # wrong suffix
            self.assertEqual(overhead.clip_for(td, "overhead5").name,
                             "swb_x_overhead5.ts")

    def test_clip_for_reports_a_missing_clip(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(P.StepError):
                P.get("overhead").clip_for(Path(td), "overhead5")

    def test_clip_for_refuses_to_guess_between_two_matches(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "a_overhead5.ts").write_bytes(b"")
            (td / "b_overhead5.ts").write_bytes(b"")
            with self.assertRaises(P.StepError):
                P.get("overhead").clip_for(td, "overhead5")

    def test_grid_dir_honours_the_explicit_override(self):
        # Resolved at use time, not at import, so setting the variable in a shell
        # before the command still takes effect.
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"STITCH_GRID_DIR": "/tmp/grids-xyz"}):
            self.assertEqual(str(P.get("underwater").still_textures),
                             "/tmp/grids-xyz")

    def test_grid_dir_falls_back_to_the_dataset_root(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"SWIM_UNDER_GRIDS_ROOT": "/tmp/ds-xyz"}):
            os.environ.pop("STITCH_GRID_DIR", None)
            self.assertEqual(str(P.get("underwater").still_textures),
                             "/tmp/ds-xyz/annotation-grids")


class AssetTest(unittest.TestCase):
    """The baked asset must reproduce the offline canvas and coverage exactly —
    it is what the GPU trusts instead of re-deriving geometry."""

    def _mesh_pair(self, td):
        """Two planes whose UVs run past the source image, so clipping and the
        ragged bottom both have something to act on."""
        return _mesh_json(td, [_mesh("left", 0.0, 1.0, 0.0, 1.0, uv=(-0.2, 0.9)),
                               _mesh("right", 0.8, 1.8, 0.0, 1.0, uv=(0.1, 1.2))])

    def test_clip_uv_shrinks_coverage_without_moving_the_canvas(self):
        from python.stitch.asset import compile_asset
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mesh = self._mesh_pair(td)
            plain = compile_asset(mesh, td / "plain.swasset", ["a", "b"], 64.0,
                                  blend_px=0.0, clip_uv=False)
            clipped = compile_asset(mesh, td / "clip.swasset", ["a", "b"], 64.0,
                                    blend_px=0.0, clip_uv=True,
                                    source_size=(32, 32))
            self.assertEqual(plain["logical_width"], clipped["logical_width"])
            self.assertEqual(plain["logical_height"], clipped["logical_height"])
            self.assertLess((td / "clip.swasset").stat().st_size,
                            (td / "plain.swasset").stat().st_size)

    def test_crop_bottom_shortens_the_canvas_without_moving_content(self):
        from python.stitch.asset import compile_asset
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mesh = self._mesh_pair(td)
            full = compile_asset(mesh, td / "full.swasset", ["a", "b"], 64.0,
                                 blend_px=0.0, crop_bottom="none")
            cropped = compile_asset(mesh, td / "crop.swasset", ["a", "b"], 64.0,
                                    blend_px=0.0, crop_bottom=8)
            self.assertEqual(cropped["crop_rows"], 8)
            self.assertEqual(cropped["logical_height"], full["logical_height"] - 8)
            # y is measured from the top of the uncropped canvas, so geometry
            # never moves: the crop only shortens the raster.
            self.assertEqual(cropped["canvas_height"], full["canvas_height"])
            self.assertEqual(cropped["encoded_height"],
                             cropped["logical_height"]
                             + (cropped["logical_height"] & 1))

    def test_crop_bottom_rejects_removing_the_whole_canvas(self):
        from python.stitch.asset import compile_asset
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with self.assertRaises(ValueError):
                compile_asset(self._mesh_pair(Path(td)), Path(td) / "x.swasset",
                              ["a", "b"], 64.0, crop_bottom=100000)

    def test_camera_count_must_match_mesh_count(self):
        from python.stitch.asset import compile_asset
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            with self.assertRaises(ValueError):
                compile_asset(self._mesh_pair(td), td / "x.swasset",
                              ["only-one"], 64.0)

    def test_camera_ids_are_written_in_mesh_order(self):
        from python.stitch.asset import compile_asset
        from python.stitch.asset_format import CAMERA, HEADER

        overhead = P.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # Real proportions: 10m and 17.5m planes overlapping 2.5m, 3m tall.
            mesh = _mesh_json(td, [
                _mesh("Plane002", 0.0, 10.0, 0.0, 3.0, tex="05-02.jpg"),
                _mesh("Plane001", 7.5, 25.0, 0.0, 3.0, tex="C06.jpg")])
            asset = td / "overhead.swasset"
            compile_asset(mesh, asset, overhead.camera_ids, overhead.ppm,
                          neg_v=overhead.neg_v, blend_px=overhead.blend_px,
                          clip_uv=overhead.clip_uv,
                          source_size=overhead.source_size,
                          crop_bottom=overhead.crop_bottom)

            data = asset.read_bytes()
            header = HEADER.unpack_from(data, 0)
            self.assertEqual(header[7], 2)                      # camera_count
            ids = [CAMERA.unpack_from(data, header[2] + index * CAMERA.size)[0]
                   .split(b"\0")[0].decode()
                   for index in range(header[7])]
            self.assertEqual(ids, ["overhead5", "overhead6"])

    def test_every_line_fits_the_runtime_camera_ceiling(self):
        # kMaxCameras is 16, sized for the underwater panorama; no line needs a
        # C++ change.
        for name in P.names():
            self.assertLessEqual(len(P.get(name).camera_ids), 16)

    def test_compile_profile_takes_every_shaping_value_from_the_line(self):
        from python.stitch import asset as A
        recorded = {}

        def spy(mesh_json, output, camera_ids, ppm, **kwargs):
            recorded.update(kwargs, ppm=ppm, camera_ids=tuple(camera_ids))
            return {"camera_count": len(camera_ids), "logical_width": 1,
                    "logical_height": 1, "encoded_width": 2, "encoded_height": 2,
                    "canvas_height": 1, "crop_rows": 0}

        original = A.compile_asset
        try:
            A.compile_asset = spy
            A.compile_profile(P.get("pool"))
        finally:
            A.compile_asset = original

        pool = P.get("pool")
        self.assertEqual(recorded["ppm"], pool.ppm)
        self.assertEqual(recorded["camera_ids"], pool.camera_ids)
        self.assertIs(recorded["neg_v"], pool.neg_v)
        self.assertIsNone(recorded["blend_px"])
        self.assertIs(recorded["clip_uv"], pool.clip_uv)
        self.assertEqual(recorded["crop_bottom"], pool.crop_bottom)
        self.assertEqual(recorded["source_size"], pool.source_size)


class AlignmentTest(unittest.TestCase):
    """Recorded clips do not share a t=0; the manifest is the only defensible
    time axis."""

    ALIGN_START, ALIGN_END, FPS = 1_000_000, 1_012_000, 30.0

    def _cams(self, **skews):
        return {camera: {"keyframe_ms": self.ALIGN_START - skew,
                         "last_decodable_ms": self.ALIGN_END, "frames": 400}
                for camera, skew in skews.items()}

    def test_start_frames_follow_the_playback_formula(self):
        from python.stitch.render_video import alignment_plan
        cams = self._cams(underA16=3083, underA1=-250)
        order = ["underA16", "underA1"]
        starts, report = alignment_plan(self.ALIGN_START, self.ALIGN_END,
                                        self.FPS, cams, order)
        self.assertEqual(starts, [int(round(3083 * self.FPS / 1000)), 0])
        self.assertEqual(report[0]["skew_ms"], 3083)
        self.assertTrue(report[1]["late_start"])

    def test_lane_offsets_put_every_lane_on_the_common_axis(self):
        from python.stitch import render_video as RV
        cams = self._cams(underA16=2964, underA1=308)
        original = RV.load_manifest
        try:
            RV.load_manifest = lambda _d: (self.ALIGN_START, self.ALIGN_END,
                                           self.FPS, cams)
            offsets = RV.lane_offsets_ms(P.get("underwater"), "ignored")
        finally:
            RV.load_manifest = original
        for camera, offset in offsets.items():
            self.assertEqual(cams[camera]["keyframe_ms"] + offset,
                             self.ALIGN_START)
        # Replaying from frame 0 instead would restart the lanes 2656ms apart,
        # which is exactly the desynchronisation the offset removes.
        raw = [cams[c]["keyframe_ms"] for c in cams]
        self.assertEqual(max(raw) - min(raw), 2656)

    def test_a_line_without_a_wall_clock_does_not_look_for_one(self):
        from python.stitch import render_video as RV
        called = []
        original = RV.load_manifest
        try:
            RV.load_manifest = lambda d: called.append(d)
            self.assertEqual(RV.lane_offsets_ms(P.get("pool"), "ignored"), {})
        finally:
            RV.load_manifest = original
        self.assertEqual(called, [])

    def test_a_missing_manifest_degrades_instead_of_failing_the_run(self):
        from python.stitch import render_video as RV
        original = RV.load_manifest
        try:
            def missing(_d):
                raise P.StepError("no manifest here")
            RV.load_manifest = missing
            for name in ("underwater", "overhead"):
                self.assertEqual(RV.lane_offsets_ms(P.get(name), "ignored"), {})
        finally:
            RV.load_manifest = original

    def test_manifest_without_an_align_window_is_fatal(self):
        from python.stitch.render_video import load_manifest
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "manifest.json").write_text(json.dumps(
                {"files": [{"source_id": "underA1", "keyframe_timestamp_ms": 1}]}))
            with self.assertRaises(P.StepError):
                load_manifest(td)

    def test_missing_manifest_names_the_escape_hatch(self):
        from python.stitch.render_video import load_manifest
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(P.StepError) as caught:
                load_manifest(td)
            self.assertIn("--no-align", str(caught.exception))

    def test_older_manifests_use_the_first_decodable_anchor(self):
        from python.stitch.render_video import load_manifest
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "manifest.json").write_text(json.dumps({
                "align_start_ms": 10, "align_end_ms": 20, "fps": 30,
                "files": [{"source_id": "underA1",
                           "first_decodable_timestamp_ms": 7}]}))
            *_unused, cams = load_manifest(td)
            self.assertEqual(cams["underA1"]["keyframe_ms"], 7)

    def test_loop_period_is_the_shortest_usable_span(self):
        from python.stitch import render_video as RV
        # spans after each aligned start: 900, 950, 880 -> the shortest wins
        cams = {"underA3": {"keyframe_ms": 0, "last_decodable_ms": 1200},
                "underA2": {"keyframe_ms": 0, "last_decodable_ms": 1150},
                "underA1": {"keyframe_ms": 0, "last_decodable_ms": 1080}}
        offsets = {"underA3": 300, "underA2": 200, "underA1": 200}
        original = RV.load_manifest
        try:
            RV.load_manifest = lambda _d: (0, 1000, 30.0, cams)
            self.assertEqual(
                RV.loop_period_ms(P.get("underwater"), "ignored", offsets), 880)
        finally:
            RV.load_manifest = original

    def test_loop_period_is_zero_without_a_manifest(self):
        from python.stitch import render_video as RV
        original = RV.load_manifest
        try:
            def missing(_d):
                raise P.StepError("none")
            RV.load_manifest = missing
            self.assertEqual(
                RV.loop_period_ms(P.get("underwater"), "ignored", {}), 0)
        finally:
            RV.load_manifest = original


class RuntimeConfigTest(unittest.TestCase):
    """The C++ loader takes camera identity from the config's declaration order,
    so the config is where a lane mix-up would become invisible."""

    def _clips(self, directory, line):
        for camera in line.camera_ids:
            (directory / f"sess_{camera}{line.clip_suffix}").write_bytes(b"")

    def test_lane_order_matches_the_asset_for_every_line(self):
        from python.stitch import run as R
        for name in P.names():
            line = P.get(name)
            with tempfile.TemporaryDirectory() as td:
                td = Path(td)
                self._clips(td, line)
                config = R.write_config(line, td / "c.conf", td, "metal",
                                        td / "o.h265", align=False)
                sources = [row.split("=", 1)[0].removeprefix("source.")
                           for row in config.read_text().splitlines()
                           if row.startswith("source.")
                           and ".start_ms" not in row]
                self.assertEqual(sources, list(line.camera_ids), name)

    def test_config_names_the_lines_own_asset_and_metrics(self):
        from python.stitch import run as R
        line = P.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._clips(td, line)
            config = R.write_config(line, td / "c.conf", td, "metal",
                                    td / "o.h265", align=False)
            rows = config.read_text().splitlines()
            self.assertIn(f"asset={line.asset.as_posix()}", rows)
            self.assertIn(f"metrics={line.metrics.as_posix()}", rows)
            self.assertIn("backend=metal", rows)

    def test_missing_clip_is_reported_not_silently_skipped(self):
        from python.stitch import run as R
        line = P.get("underwater")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for camera in line.camera_ids[1:]:            # underA16 absent
                (td / f"sess_{camera}.ts").write_bytes(b"")
            with self.assertRaises(P.StepError):
                R.write_config(line, td / "c.conf", td, "metal", td / "o.h265",
                               align=False)

    def test_no_start_offsets_when_alignment_is_disabled(self):
        from python.stitch import run as R
        line = P.get("underwater")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._clips(td, line)
            config = R.write_config(line, td / "c.conf", td, "metal",
                                    td / "o.h265", align=False)
            self.assertNotIn("start_ms", config.read_text())

    def test_loop_controls_flip_together(self):
        from python.stitch import run as R
        line = P.get("underwater")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            self._clips(td, line)
            on = R.write_config(line, td / "on.conf", td, "metal",
                                td / "o.h265", align=False).read_text()
            off = R.write_config(line, td / "off.conf", td, "metal",
                                 td / "o.h265", align=False,
                                 loop=False).read_text()
        self.assertIn("loop_sources=true", on)
        self.assertIn("stop_at_eof=false", on)
        self.assertIn("loop_sources=false", off)
        self.assertIn("stop_at_eof=true", off)

    def test_platform_selects_toolchain_build_dir_and_executable(self):
        from python.stitch import run as R
        original = R.platform.system
        try:
            R.platform.system = lambda: "Darwin"
            self.assertEqual(R.default_backend(), "metal")
            self.assertEqual(R.build_dir_for("metal").name, "metal-release")
            self.assertEqual(
                R.executable_for(R.build_dir_for("metal")).name, "swim_realtime")

            R.platform.system = lambda: "Windows"
            self.assertEqual(R.default_backend(), "d3d11")
            self.assertEqual(R.build_dir_for("d3d11").name, "win-d3d11")
            self.assertEqual(
                R.executable_for(R.build_dir_for("d3d11")).name,
                "swim_realtime.exe")
        finally:
            R.platform.system = original

    def test_newer_than_treats_a_missing_target_as_stale(self):
        from python.stitch import run as R
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            source = td / "src"
            source.write_text("x")
            self.assertFalse(R.newer_than(td / "absent", source))
            target = td / "target"
            target.write_text("y")
            self.assertTrue(R.newer_than(target, source))

    def test_build_inputs_cover_the_config_loader_and_cmake(self):
        # The stale-exe symptom this guards against is remote from its cause: an
        # exe older than config.cpp rejects a freshly written config's newest key
        # as `unknown key`, blaming the config file.
        from python.stitch import run as R
        inputs = set(R.build_inputs())
        for required in (ROOT / "CMakeLists.txt",
                         ROOT / "cpp" / "core" / "src" / "config.cpp",
                         ROOT / "cpp" / "core" / "include" / "swim" / "core"
                         / "config.hpp"):
            self.assertIn(required, inputs)
        # Every backend's sources count, whichever one this run selects: the exe
        # links all of them that were available at configure time.
        for backend in ("metal", "d3d11", "cudagl"):
            self.assertTrue(
                any(f"backends{os.sep}{backend}{os.sep}" in str(path)
                    for path in inputs), backend)

    def test_build_step_rebuilds_when_a_source_is_newer_than_the_exe(self):
        import argparse
        from python.stitch import run as R
        line = P.get("pool")
        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "swim_realtime"
            exe.write_text("stale")
            source = Path(td) / "config.cpp"
            source.write_text("newer")
            os.utime(source, (exe.stat().st_mtime + 10,) * 2)
            built = []
            originals = (R.executable_for, R.build_inputs, R.run,
                         R.copy_runtime_dlls)
            try:
                R.executable_for = lambda _build_dir: exe
                R.build_inputs = lambda: [source]
                R.run = lambda command, **_kwargs: built.append(command)
                R.copy_runtime_dlls = lambda _destination: None
                args = argparse.Namespace(backend="metal", force=False)
                R.step_build(line, args)
                self.assertTrue(built, "a newer source must trigger a build")
                # And the same tree is skipped once the exe is the newer file.
                built.clear()
                os.utime(exe, (source.stat().st_mtime + 10,) * 2)
                R.step_build(line, args)
                self.assertEqual(built, [])
            finally:
                (R.executable_for, R.build_inputs, R.run,
                 R.copy_runtime_dlls) = originals

    def test_shaping_stamp_is_per_line_and_covers_every_bake_option(self):
        # mtime cannot see a changed --ppm, so the stamp is what makes the asset
        # step skip correctly.
        import argparse
        from python.stitch import run as R
        blank = argparse.Namespace(ppm=None, blend_px=None, crop_bottom=None)
        stamps = {name: R.shaping_stamp(P.get(name), blank) for name in P.names()}
        self.assertEqual(len(set(stamps.values())), len(stamps))
        for name, stamp in stamps.items():
            self.assertTrue(stamp.startswith(name + " "))
        overhead = R.shaping_stamp(
            P.get("overhead"),
            argparse.Namespace(ppm=999.0, blend_px=None, crop_bottom=None))
        self.assertIn("999.0", overhead)
        self.assertNotEqual(overhead, stamps["overhead"])

    def test_stamp_files_never_collide(self):
        from python.stitch import run as R
        paths = [R.stamp_path(P.get(name)) for name in P.names()]
        self.assertEqual(len(set(paths)), len(paths))
        self.assertTrue(all(path.suffix == ".stamp" for path in paths))


class RefTexTest(unittest.TestCase):
    """Reference textures are named after the camera, not the mesh's texture
    basename: 05-02.jpg and C06.jpg say nothing about which camera they are, and
    reusing a .jpg name would re-encode a lossless frame."""

    def test_names_follow_camera_ids(self):
        from python.stitch import export_ref_tex as E
        self.assertEqual(E.tex_names(P.get("overhead")),
                         ["overhead5.png", "overhead6.png"])
        names = E.tex_names(P.get("underwater"))
        self.assertEqual((names[0], names[-1], len(names)),
                         ("underA16.png", "underA1.png", 16))

    def test_a_video_source_needs_a_clip_directory(self):
        from python.stitch import export_ref_tex as E
        with self.assertRaises(P.StepError):
            E.export(P.get("overhead"), video_dir=None)

    def test_one_png_per_camera_from_clip_frame_zero(self):
        import cv2
        from python.stitch import export_ref_tex as E
        overhead = P.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            clips = td / "clips"
            clips.mkdir()
            for index, camera in enumerate(overhead.camera_ids):
                writer = cv2.VideoWriter(str(clips / f"sess_{camera}.ts"),
                                         cv2.VideoWriter_fourcc(*"mp4v"),
                                         30.0, (32, 16))
                writer.write(np.full((16, 32, 3), 40 * (index + 1), np.uint8))
                writer.release()
            written = E.export(overhead, out_dir=td / "ref_tex", video_dir=clips)
        self.assertEqual([path.name for path in written],
                         ["overhead5.png", "overhead6.png"])

    def test_an_unreadable_clip_is_reported_not_written_black(self):
        from python.stitch import export_ref_tex as E
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for camera in P.get("overhead").camera_ids:
                (td / f"sess_{camera}.ts").write_bytes(b"not a video")
            with self.assertRaises(Exception):
                E.export(P.get("overhead"), out_dir=td / "out", video_dir=td)


class DispatcherTest(unittest.TestCase):
    """One step table, the line as an argument — a fourth line costs a profile
    record and no new CLI surface."""

    def test_step_table_lists_offline_then_realtime(self):
        from python.stitch import __main__ as cli
        self.assertEqual(list(cli.STEPS),
                         ["extract", "tex", "still", "video", "asset",
                          "build", "live"])

    def test_every_step_takes_the_line_and_the_args(self):
        import inspect
        from python.stitch import __main__ as cli
        for name, handler in cli.STEPS.items():
            self.assertEqual(list(inspect.signature(handler).parameters),
                             ["profile", "args"], name)

    def test_unknown_step_lists_the_valid_ones(self):
        from python.stitch import __main__ as cli
        with self.assertRaises(SystemExit) as caught:
            cli.main(["overhead", "polish"])
        message = str(caught.exception)
        self.assertIn("polish", message)
        self.assertIn("extract", message)

    def test_unknown_line_is_rejected_before_any_step_runs(self):
        from python.stitch import __main__ as cli
        with self.assertRaises(SystemExit) as caught:
            cli.main(["nosuchline", "extract"])
        self.assertIn("nosuchline", str(caught.exception))

    def test_steps_run_in_the_order_given(self):
        from python.stitch import __main__ as cli
        order = []
        original = dict(cli.STEPS)
        try:
            for name in ("extract", "asset"):
                cli.STEPS[name] = (
                    lambda profile, args, _name=name: order.append(_name))
            cli.main(["overhead", "asset,extract"])
        finally:
            cli.STEPS.clear()
            cli.STEPS.update(original)
        self.assertEqual(order, ["asset", "extract"])

    def test_video_and_live_require_clips(self):
        from python.stitch import __main__ as cli
        for step in ("video", "live"):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["overhead", step])
            self.assertIn("--video-dir", str(caught.exception))

    def test_pool_falls_back_to_its_dataset_directory(self):
        # pool's clips are a machine-wide session, so `video` must not demand
        # --video-dir the way the per-sample plane lines do.
        from python.stitch import __main__ as cli
        args = cli.parse_args(["pool", "video"])
        self.assertIsNone(args.video_dir)
        self.assertIsNotNone(P.default_video_dir(P.get("pool")))

    def test_tex_requires_clips_only_when_the_source_is_video(self):
        from python.stitch import __main__ as cli
        with self.assertRaises(SystemExit) as caught:
            cli.main(["overhead", "tex"])
        self.assertIn("--video-dir", str(caught.exception))


class RenderTest(unittest.TestCase):
    """One renderer serves the designer's calibration textures and the
    camera-named reference exports."""

    def _line(self, td, **overrides):
        """A throwaway line pointing at a temporary directory."""
        import dataclasses
        base = P.get("overhead")
        return dataclasses.replace(base, _out_dir=Path(td), **overrides)

    def _fixture(self, td, value=200, size=(16, 32)):
        import cv2
        line = self._line(td, tex_dir=Path(td), still_tex_dir=Path(td))
        _mesh_json(td, [_mesh("Plane002", 0.0, 10.0, 0.0, 3.0, tex="05-02.jpg"),
                        _mesh("Plane001", 7.5, 25.0, 0.0, 3.0, tex="C06.jpg")])
        for name in ("05-02.jpg", "C06.jpg", "overhead5.png", "overhead6.png"):
            cv2.imwrite(str(Path(td) / name),
                        np.full((*size, 3), value, np.uint8))
        return line

    def test_writes_still_grid_and_heatmap(self):
        from python.stitch import render as R
        with tempfile.TemporaryDirectory() as td:
            line = self._fixture(td)
            width, height = R.render(line, None, Path(td) / "out", ppm=8.0)
            for suffix in ("", "_grid", "_heat"):
                self.assertTrue((Path(td) / f"out{suffix}.png").is_file(), suffix)
            import cv2
            image = cv2.imread(str(Path(td) / "out.png"))
            self.assertEqual((image.shape[1], image.shape[0]), (width, height))
            self.assertGreater(int(image.max()), 0)           # not all black

    def test_positional_names_render_the_same_as_basenames(self):
        import cv2
        from python.stitch import render as R
        with tempfile.TemporaryDirectory() as td:
            line = self._fixture(td)
            R.render(line, None, Path(td) / "by_basename", ppm=8.0,
                     grid=False, heatmap=False)
            R.render(line, None, Path(td) / "by_camera", ppm=8.0,
                     grid=False, heatmap=False,
                     tex_names=["overhead5.png", "overhead6.png"])
            self.assertTrue(np.array_equal(
                cv2.imread(str(Path(td) / "by_basename.png")),
                cv2.imread(str(Path(td) / "by_camera.png"))))

    def test_a_wrong_number_of_texture_names_is_refused(self):
        from python.stitch import render as R
        with tempfile.TemporaryDirectory() as td:
            line = self._fixture(td)
            with self.assertRaises(P.StepError) as caught:
                R.render(line, None, Path(td) / "out", ppm=8.0,
                         tex_names=["overhead5.png"])
            self.assertIn("1", str(caught.exception))
            self.assertIn("2", str(caught.exception))


    def test_full_res_rescales_back_to_the_source_height(self):
        from python.stitch import render as R
        import cv2
        with tempfile.TemporaryDirectory() as td:
            # A short second plane leaves a ragged bottom for the auto-crop.
            line = self._line(td, tex_dir=Path(td), still_tex_dir=Path(td),
                              full_res=True, crop_bottom="auto")
            _mesh_json(td, [_mesh("tall", 0.0, 4.0, 0.0, 3.0, tex="a.png"),
                            _mesh("short", 3.0, 8.0, 0.5, 3.0, tex="b.png")])
            for name in ("a.png", "b.png"):
                cv2.imwrite(str(Path(td) / name), np.full((24, 32, 3), 180, np.uint8))
            _width, height = R.render(line, None, Path(td) / "out",
                                      grid=False, heatmap=False)
            self.assertEqual(height, 24)

    def test_a_missing_texture_directory_names_itself(self):
        from python.stitch import render as R
        with tempfile.TemporaryDirectory() as td:
            line = self._fixture(td)
            with self.assertRaises(P.StepError) as caught:
                R.render(line, Path(td) / "absent", Path(td) / "out", ppm=8.0)
            self.assertIn("absent", str(caught.exception))


class VideoTest(unittest.TestCase):
    def test_camera_count_must_match_mesh_count(self):
        import dataclasses
        from python.stitch import render_video as RV
        with tempfile.TemporaryDirectory() as td:
            line = dataclasses.replace(P.get("overhead"), _out_dir=Path(td),
                                       camera_ids=("overhead5",))
            _mesh_json(td, [_mesh("a", 0.0, 1.0, tex="05-02.jpg"),
                            _mesh("b", 0.9, 2.0, tex="C06.jpg")])
            with self.assertRaises(P.StepError) as caught:
                RV.render(line, td, Path(td) / "out.mp4")
            message = str(caught.exception)
            self.assertIn("1", message)
            self.assertIn("2", message)

    def test_clip_lookup_goes_through_the_line(self):
        # render must not glob for clips itself; the line owns the suffix and the
        # ambiguity rules.
        import dataclasses
        from python.stitch import render_video as RV
        asked = []

        class Recording(type(P.get("overhead"))):
            pass

        line = dataclasses.replace(P.get("overhead"))
        with tempfile.TemporaryDirectory() as td:
            line = dataclasses.replace(line, _out_dir=Path(td))
            _mesh_json(td, [_mesh("a", 0.0, 1.0, tex="05-02.jpg"),
                            _mesh("b", 0.9, 2.0, tex="C06.jpg")])
            object.__setattr__(line, "clip_for",
                               lambda d, c: asked.append(c) or Path(td) / "x.ts")
            with self.assertRaises(P.StepError):
                RV.render(line, td, Path(td) / "out.mp4")
        self.assertEqual(asked[:1], ["overhead5"])


class DocsTest(unittest.TestCase):
    def test_readme_names_every_line_and_its_entry_point(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("run_stitch.sh", readme)
        for name in P.names():
            self.assertIn(name, readme)

    def test_readme_has_no_retired_commands(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for retired in ("uw-extract", "uw-tex", "uw-render", "uw-real",
                        "uw-video", "run_underwater.sh", "run_metal.sh",
                        "run_python.sh", "python.underwater",
                        "python.validation", "python.assets",
                        "python.annotation_preview", "underwater_16.swasset"):
            self.assertNotIn(retired, readme, f"README still mentions {retired}")


class PatchGridTest(unittest.TestCase):
    """The missing calibration line is derived from the grid the present lines
    form, not eyeballed: snap each line to a 0.5m slot, report the empty slot."""

    def test_finds_the_empty_grid_slot(self):
        from python.stitch.patch_grid import missing_world_x
        present = [0.0, 0.5, 1.0, 2.0, 2.5, 3.0]          # slot at 1.5 absent
        columns = [int(round(x * 100.0)) for x in present]
        gaps, residual = missing_world_x(columns, 0.0, 100.0)
        self.assertEqual([round(x, 3) for x in gaps], [1.5])
        self.assertLess(residual, 0.01)

    def test_no_gap_reports_nothing(self):
        from python.stitch.patch_grid import missing_world_x
        gaps, _residual = missing_world_x([0, 50, 100, 150], 0.0, 100.0)
        self.assertEqual(gaps, [])

    def test_lines_off_the_grid_are_refused(self):
        from python.stitch.patch_grid import missing_world_x
        with self.assertRaises(P.StepError):
            missing_world_x([0, 31, 62, 93], 0.0, 100.0)   # 0.31m fits no 0.5m grid

    def test_refuses_to_patch_inside_an_overlap(self):
        # A gap both planes reach is covered by the neighbour, so painting one
        # plane's texture would be wrong.
        from python.stitch.patch_grid import owning_mesh
        left = _mesh("left", 0.0, 10.0)
        right = _mesh("right", 7.5, 17.5)
        self.assertEqual(owning_mesh([left, right], 2.0)["node"], "left")
        self.assertEqual(owning_mesh([left, right], 15.0)["node"], "right")
        with self.assertRaises(P.StepError):
            owning_mesh([left, right], 8.0)

    def test_line_columns_ignores_short_marks(self):
        from python.stitch.patch_grid import line_columns
        image = np.zeros((100, 60, 3), np.uint8)
        image[:, 10:13] = (1, 254, 254)        # full-height line
        image[:8, 30:33] = (1, 254, 254)       # a short mark, not a grid line
        image[:, 50:53] = (1, 254, 254)        # full-height line
        self.assertEqual(line_columns(image), [11, 51])


class FbxIntegrationTest(unittest.TestCase):
    """Extraction against the real models — skipped where they are not present."""

    @unittest.skipUnless(HAS_FBX, "FBX SDK not available")
    @unittest.skipUnless(POOL_FBX.is_file(), "pool.fbx not present")
    def test_pool_keeps_its_declared_two_row_order(self):
        from python.stitch import extract as E
        import dataclasses
        with tempfile.TemporaryDirectory() as td:
            line = dataclasses.replace(P.get("pool"), _out_dir=Path(td))
            meshes = E.extract(line)
        self.assertEqual([m["node"] for m in meshes],
                         ["01", "02", "03", "u", "Plane004", "Plane007"])
        # World-X order would be Plane007, 01, 02, Plane004, 03, u — which pairs
        # cam3 with the opposite bank's plane.
        self.assertNotEqual([m["node"] for m in E.sort_by_world_x(meshes)],
                            [m["node"] for m in meshes])

    @unittest.skipUnless(HAS_FBX, "FBX SDK not available")
    @unittest.skipUnless(OVERHEAD_FBX.is_file(), "002.fbx not present")
    def test_overhead_orders_two_planes_left_to_right(self):
        from python.stitch import extract as E
        import dataclasses
        with tempfile.TemporaryDirectory() as td:
            line = dataclasses.replace(P.get("overhead"), _out_dir=Path(td))
            meshes = E.extract(line)
        self.assertEqual([m["node"] for m in meshes], ["Plane002", "Plane001"])
        # which pins overhead5 -> 05-02.jpg and overhead6 -> C06.jpg positionally
        self.assertEqual([m["texture_basename"] for m in meshes],
                         ["05-02.jpg", "C06.jpg"])

    @unittest.skipUnless(HAS_FBX, "FBX SDK not available")
    @unittest.skipUnless(OVERHEAD_FBX.is_file(), "002.fbx not present")
    def test_overhead_covers_the_same_lane_as_the_underwater_panorama(self):
        # Both models cover the same 25.000m x 3.000m lane, which is why one set
        # of geometry code serves both.
        from python.stitch import extract as E
        import dataclasses
        with tempfile.TemporaryDirectory() as td:
            line = dataclasses.replace(P.get("overhead"), _out_dir=Path(td))
            meshes = E.extract(line)
        xs = [v["pos"][0] for m in meshes for t in m["triangles"] for v in t]
        ys = [v["pos"][1] for m in meshes for t in m["triangles"] for v in t]
        self.assertAlmostEqual(max(xs) - min(xs), 25.0, places=3)
        self.assertAlmostEqual(max(ys) - min(ys), 3.0, places=3)

    @unittest.skipUnless(HAS_FBX, "FBX SDK not available")
    @unittest.skipUnless(POOL_FBX.is_file(), "pool.fbx not present")
    def test_a_camera_count_mismatch_stops_extraction(self):
        import dataclasses
        from python.stitch import extract as E
        with tempfile.TemporaryDirectory() as td:
            line = dataclasses.replace(P.get("pool"), _out_dir=Path(td),
                                       camera_ids=("cam1", "cam2"))
            with self.assertRaises(P.StepError) as caught:
                E.extract(line)
            self.assertIn("6", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
