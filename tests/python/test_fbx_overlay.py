"""Tests for normalized FBX UV mesh overlays."""
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from python.common.media import MediaError
from python.fbx_overlay.classify import (MeshKind, classify_mesh)
from python.fbx_overlay.meters import (annotate_document, grid_annotation,
                                       inline_vertex_meters, label_anchors,
                                       label_anchors_world)
from python.fbx_overlay.render import (OverlayError, draw_mesh, draw_meshes,
                                       draw_meter_labels, load_texture,
                                       uv_to_pixel)


ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "inputs/water_entry/background.jpg"
# The pre-femto one-mesh-per-FBX models, now the water_entry legacy line.
LEGACY_MESHES = (
    (ROOT / "inputs/water_entry/models/006.fbx", "Plane004", 140),
    (ROOT / "inputs/water_entry/models/005.fbx", "Plane005", 60),
)
# The water_entry2 line's sub-cameras: name -> (fbx, [(node, triangles, kind)]).
CAMERAS = {
    "femto": (ROOT / "inputs/water_entry/models/femto.fbx", (
        ("Plane006", 140, MeshKind.VERTICAL),
        ("Plane010", 80, MeshKind.SURFACE),
        ("Rectangle004", 2, MeshKind.FULL_FRAME),
    )),
    "gemini": (ROOT / "inputs/water_entry/models/gemini.fbx", (
        ("Plane007", 126, MeshKind.VERTICAL),
        ("Plane009", 64, MeshKind.SURFACE),
        ("Rectangle005", 2, MeshKind.FULL_FRAME),
    )),
}
# The overhead/overhead2 canvas lines: name -> (fbx, [(node, triangles)]).
OVERHEAD_LINES = {
    "overhead": (ROOT / "inputs/overhead/models/002.fbx", (
        ("Plane001", 204),
        ("Plane002", 120),
    )),
    "overhead2": (ROOT / "inputs/overhead/models/25 水面.fbx", (
        ("Plane002", 200),
        ("Plane011", 340),
    )),
}


def _triangle(uv=((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))):
    return {"triangles": [[{"uv": [u, v]} for u, v in uv]]}


def _mesh(triangles, uvs):
    """A mesh dict whose UV bounding box EXACTLY spans the given ranges.

    The first triangle pins the corners — the boundary vertices must be present
    or the measured du/dv will come up short. Extra triangles only add density.
    """
    u0, u1, v0, v1 = uvs
    built = [[{"uv": [u0, v0]}, {"uv": [u1, v0]}, {"uv": [u0, v1]}]]
    for index in range(1, triangles):
        built.append([
            {"uv": [
                u0 + (u1 - u0) * (((index * 5 + corner * 7) % 17) / 17.0),
                v0 + (v1 - v0) * (((index * 3 + corner * 11) % 13) / 13.0),
            ]}
            for corner in range(3)
        ])
    return {"triangles": built}


def _grid_mesh(xs, ys, uv_of=lambda x, y: (0.0, 0.0)):
    """A mesh with a degenerate triangle per ``(x, y)`` grid point.

    Enough for meters.py (reads vertex pos only) and for label placement
    (uv comes from ``uv_of``).
    """
    triangles = []
    for x in xs:
        for y in ys:
            uv = uv_of(x, y)
            vertex = {"pos": [x, y], "uv": [uv[0], uv[1]]}
            triangles.append([vertex, dict(vertex), dict(vertex)])
    return {"triangles": triangles}


_VERTICAL_XS = [-2.5, -2.0, -1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
_VERTICAL_YS = [1.629714, 1.879714, 2.129714, 2.379714, 2.629714,
                2.879714, 3.129714, 3.379714]
_SURFACE_XS = [-2.5, -1.5, -0.5, 0.5, 1.5, 2.5]
_SURFACE_YS = [0.665257, 0.865257, 3.165257, 3.365257]


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


class ClassifierTest(unittest.TestCase):
    def test_full_frame_quad(self):
        # The camera-quad mesh: 2 triangles spanning the whole [0,1]² UV space.
        mesh = _mesh(2, (0.0, 1.0, 0.0, 1.0))
        self.assertIs(classify_mesh(mesh), MeshKind.FULL_FRAME)

    def test_full_frame_tolerates_small_overshoot(self):
        # Measured UVs overshoot 1.0 slightly (e.g. u to 1.002).
        mesh = _mesh(2, (-0.001, 1.002, -0.002, 1.0))
        self.assertIs(classify_mesh(mesh), MeshKind.FULL_FRAME)

    def test_vertical_water(self):
        # 140-tri mesh spanning v≈0-0.5 (bottom into mid image).
        mesh = _mesh(140, (0.0, 0.84, 0.0, 0.5))
        self.assertIs(classify_mesh(mesh), MeshKind.VERTICAL)

    def test_water_surface(self):
        # 30-tri mesh, thin v band at the image bottom.
        mesh = _mesh(30, (0.0, 1.0, 0.0, 0.14))
        self.assertIs(classify_mesh(mesh), MeshKind.SURFACE)

    def test_overhead_plane(self):
        # 200-tri mesh, thin v band starting mid-image (the overhead plane).
        mesh = _mesh(200, (0.49, 0.94, 0.46, 0.62))
        self.assertIs(classify_mesh(mesh), MeshKind.PLANE)

    def test_small_mid_image_band_is_surface_not_plane(self):
        # High v_min but only 30 tris — too small to be an overhead plane.
        mesh = _mesh(30, (0.1, 0.9, 0.5, 0.6))
        self.assertIs(classify_mesh(mesh), MeshKind.SURFACE)

    def test_dense_bottom_band_is_surface_not_plane(self):
        # 200 tris but touching the image bottom (v_min ≈ 0) — the v_min guard
        # keeps a future dense water-surface rebuild from flipping to PLANE.
        mesh = _mesh(200, (0.0, 1.0, 0.0, 0.14))
        self.assertIs(classify_mesh(mesh), MeshKind.SURFACE)

    def test_dv_boundary_is_vertical(self):
        # dv == 0.3 exactly is a vertical mesh (inclusive boundary).
        mesh = _mesh(10, (0.0, 1.0, 0.0, 0.3))
        self.assertIs(classify_mesh(mesh), MeshKind.VERTICAL)

    def test_small_non_quad_mesh_falls_back_to_vertical(self):
        # 4 triangles, thin band — not full-frame, so it must not crash.
        mesh = _mesh(4, (0.1, 0.2, 0.1, 0.2))
        self.assertIs(classify_mesh(mesh), MeshKind.VERTICAL)

    def test_empty_mesh_falls_back_to_vertical(self):
        # No triangles at all: cannot be full-frame, must not crash. The
        # small-mesh branch (<= 4 triangles) falls back to VERTICAL.
        mesh = {"triangles": []}
        self.assertIs(classify_mesh(mesh), MeshKind.VERTICAL)


class TextureLoadingTest(unittest.TestCase):
    def test_load_texture_returns_array_for_absolute_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pixel.png"
            cv2.imwrite(str(path), np.full((8, 12, 3), 128, dtype=np.uint8))
            image = load_texture({"texture": str(path)})
            self.assertEqual(image.shape, (8, 12, 3))

    def test_load_texture_resolves_repo_relative_display_path(self):
        # extract_mesh returns display() paths, relative to the repo root.
        reference = "inputs/water_entry/models/femto.fbm/333.jpg"
        if not (ROOT / reference).is_file():
            self.skipTest("femto .fbm texture not available")
        image = load_texture({"texture": reference})
        self.assertEqual(image.shape[:2], (720, 1280))

    def test_missing_texture_raises(self):
        mesh = {"texture": "inputs/water_entry/models/femto.fbm/does-not-exist.jpg"}
        with self.assertRaises(MediaError) as raised:
            load_texture(mesh)
        self.assertIn("gitignored", str(raised.exception))


class MetersTest(unittest.TestCase):
    def test_vertical_columns_skip_rightmost(self):
        mesh = _grid_mesh(_VERTICAL_XS, _VERTICAL_YS)
        mesh["kind"] = MeshKind.VERTICAL
        grid = grid_annotation(mesh)
        x = grid["x"]
        self.assertEqual(len(x), 10)                     # 11 columns, rightmost skipped
        self.assertEqual(x[0]["x"], -2.5)
        self.assertEqual(x[-1]["x"], 2.0)                # right-2 kept, 2.5 skipped
        self.assertEqual([e["meter"] for e in x],
                         [5.0, 4.5, 4.0, 3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5])

    def test_vertical_rows_skip_bottom_and_top(self):
        mesh = _grid_mesh(_VERTICAL_XS, _VERTICAL_YS)
        mesh["kind"] = MeshKind.VERTICAL
        y = grid_annotation(mesh)["y"]
        self.assertEqual(len(y), 6)                      # 8 rows, bottom+top skipped
        self.assertEqual([e["meter"] for e in y],
                         [0.0, 0.25, 0.5, 0.75, 1.0, 1.25])
        self.assertEqual(y[0]["y"], _VERTICAL_YS[1])
        self.assertEqual(y[-1]["y"], _VERTICAL_YS[-2])

    def test_surface_columns_and_bands(self):
        mesh = _grid_mesh(_SURFACE_XS, _SURFACE_YS)
        mesh["kind"] = MeshKind.SURFACE
        grid = grid_annotation(mesh)
        self.assertEqual([e["meter"] for e in grid["x"]],
                         [4.5, 3.5, 2.5, 1.5, 0.5])      # 6 cols, rightmost skipped, step 1.0
        self.assertEqual(grid["x"][-1]["x"], 1.5)        # right-2 = 0.5m
        self.assertEqual([(e["y"], e["meter"]) for e in grid["y"]],
                         [(_SURFACE_YS[0], 0.0), (_SURFACE_YS[1], 0.0),
                          (_SURFACE_YS[2], 2.5), (_SURFACE_YS[3], 2.5)])

    def test_step_is_measured_from_the_mesh(self):
        # A vertical grid with a 0.5 m column step must produce 0.5 m meters
        # even though the anchor is still right-2 = 0.5.
        xs = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]              # 6 columns
        ys = [0.0, 0.25, 0.5, 0.75]                      # 4 rows
        mesh = _grid_mesh(xs, ys)
        mesh["kind"] = MeshKind.VERTICAL
        grid = grid_annotation(mesh)
        self.assertEqual([e["meter"] for e in grid["x"]],
                         [2.5, 2.0, 1.5, 1.0, 0.5])

    def test_full_frame_raises(self):
        mesh = _grid_mesh([-2.5, 2.5], [1.6, 4.4])
        mesh["kind"] = MeshKind.FULL_FRAME
        with self.assertRaises(ValueError):
            grid_annotation(mesh)

    def test_empty_mesh_returns_empty_arrays(self):
        mesh = {"triangles": [], "kind": MeshKind.VERTICAL}
        grid = grid_annotation(mesh)
        self.assertEqual(grid, {"x": [], "y": []})

    def test_surface_labels_dedupe_bands(self):
        # The surface's two bands share the same X meters; each X meter must be
        # labeled ONCE, and each band's Y meter once (not per edge row).
        mesh = _grid_mesh(_SURFACE_XS, _SURFACE_YS)
        mesh["kind"] = MeshKind.SURFACE
        grid = grid_annotation(mesh)
        anchors = label_anchors(mesh, grid)
        texts = [text for _uv, text, _side in anchors]
        # 5 X meters + 2 distinct Y meters (0.0 and 2.5).
        self.assertEqual(len(anchors), 7)
        self.assertEqual(texts.count("2.5"), 2)    # one X column + one band, not three
        self.assertEqual(texts.count("0"), 1)
        # X labels drawn above the gridline; Y labels to the LEFT (right side of
        # the image).
        x_texts = [text for _uv, text, side in anchors if side == "above"]
        y_texts = [text for _uv, text, side in anchors if side == "left"]
        self.assertEqual(len(x_texts), 5)
        self.assertEqual(len(y_texts), 2)

    def test_y_labels_anchor_at_right_end(self):
        # Y labels must anchor at the row's RIGHT end (max u) so they run down
        # the image's right side (the label text itself is drawn to the left).
        def uv_of(x, y):
            return ((x + 2.5) / 5.0, (y - 1.6) / 2.0)   # realistic spread
        mesh = _grid_mesh(_VERTICAL_XS, _VERTICAL_YS, uv_of)
        mesh["kind"] = MeshKind.VERTICAL
        grid = grid_annotation(mesh)
        anchors = label_anchors(mesh, grid)
        y_anchors = [uv for uv, _text, side in anchors if side == "left"]
        self.assertEqual(len(y_anchors), 6)
        # Each Y anchor sits at the row's right end: u = max u of that row,
        # which for this grid is the rightmost column (x=2.5) -> u = 1.0.
        for uv in y_anchors:
            self.assertAlmostEqual(uv[0], 1.0, places=6)

    def test_vertical_labels_one_per_column_and_row(self):
        mesh = _grid_mesh(_VERTICAL_XS, _VERTICAL_YS)
        mesh["kind"] = MeshKind.VERTICAL
        grid = grid_annotation(mesh)
        anchors = label_anchors(mesh, grid)
        self.assertEqual(len(anchors), 10 + 6)     # 10 X + 6 Y

    def test_plane_columns_world_difference_no_skip(self):
        # Overhead X meters are world-coordinate differences from the pool's
        # rightmost X; NO column is skipped (unlike vertical/surface).
        xs = list(range(-35, -9))                    # -35..-10, step 1
        ys = [31.94, 32.19, 33.065, 33.815, 34.69, 34.94]
        mesh = _grid_mesh(xs, ys)
        mesh["kind"] = MeshKind.PLANE
        grid = grid_annotation(mesh, rightmost_x=-10)
        self.assertEqual(len(grid["x"]), len(xs))    # no skip
        self.assertEqual(grid["x"][0]["meter"], 25.0)   # -35 -> 25m
        self.assertEqual(grid["x"][-1]["meter"], 0.0)   # -10 -> 0m
        self.assertEqual(grid["x"][-2]["meter"], 1.0)

    def test_plane_columns_fallback_to_mesh_max(self):
        # Without rightmost_x, a single plane uses its own max x.
        mesh = _grid_mesh([-5.0, -4.0, -3.0], [0.0])
        mesh["kind"] = MeshKind.PLANE
        grid = grid_annotation(mesh)
        self.assertEqual([e["meter"] for e in grid["x"]], [2.0, 1.0, 0.0])

    def test_plane_rows_world_difference(self):
        ys = [31.94, 32.19, 33.065, 33.815, 34.69, 34.94]
        mesh = _grid_mesh([-10.0], ys)
        mesh["kind"] = MeshKind.PLANE
        grid = grid_annotation(mesh)
        self.assertEqual([(round(e["y"], 3), e["meter"]) for e in grid["y"]],
                         [(32.19, 0.0), (33.065, 0.875),
                          (33.815, 1.625), (34.69, 2.5)])

    def test_label_anchors_world_positions(self):
        def uv_of(x, y):
            return ((x + 2.5) / 5.0, 0.5)
        mesh = _grid_mesh(_VERTICAL_XS, _VERTICAL_YS, uv_of)
        mesh["kind"] = MeshKind.VERTICAL
        grid = grid_annotation(mesh)
        anchors = label_anchors_world(mesh, grid)
        x_anchors = [a for a in anchors if a[3] == "above"]
        y_anchors = [a for a in anchors if a[3] == "left"]
        self.assertEqual(len(x_anchors), 10)
        self.assertEqual(len(y_anchors), 6)
        # X anchors at the column's physical top edge (max y); Y at the row's
        # right end (max x) — mirrors label_anchors' UV placement.
        for wx, wy, _text, side in x_anchors:
            self.assertAlmostEqual(wy, _VERTICAL_YS[-1], places=3)
        for wx, wy, _text, side in y_anchors:
            self.assertAlmostEqual(wx, _VERTICAL_XS[-1], places=3)


class MetersDocumentTest(unittest.TestCase):
    def _document(self):
        meshes = [
            _grid_mesh(_VERTICAL_XS, _VERTICAL_YS),
            _grid_mesh(_SURFACE_XS, _SURFACE_YS),
            _grid_mesh([-2.5, 2.5], [1.6, 4.4]),
        ]
        meshes[0]["node"] = "Plane006"
        meshes[0]["kind"] = MeshKind.VERTICAL
        meshes[1]["node"] = "Plane010"
        meshes[1]["kind"] = MeshKind.SURFACE
        meshes[2]["node"] = "Rectangle004"
        meshes[2]["kind"] = MeshKind.FULL_FRAME
        return annotate_document("femto", "inputs/.../femto.fbx", meshes)

    def test_structure_and_kind_is_string(self):
        doc = self._document()
        self.assertEqual(doc["source"], "inputs/.../femto.fbx")
        self.assertEqual(doc["camera"], "femto")
        self.assertEqual([m["node"] for m in doc["meshes"]],
                         ["Plane006", "Plane010"])       # FULL_FRAME excluded
        for entry in doc["meshes"]:
            self.assertIsInstance(entry["kind"], str)
            self.assertNotIn("grid", entry)              # meters are in vertices

    def test_document_round_trips_through_json(self):
        doc = self._document()
        text = json.dumps(doc)
        loaded = json.loads(text)
        self.assertEqual(loaded["camera"], "femto")
        self.assertEqual(loaded["meshes"][0]["kind"], "vertical")

    def test_vertices_carry_meter(self):
        # The synthetic vertical grid (11 cols, 8 rows) maps to 10 kept columns
        # (x meters 5.0..0.5) and 6 kept rows (y meters 0.0..1.25). Vertices on
        # the skipped rightmost column / bottom / top rows have no meter key.
        def uv_of(x, y):
            return ((x + 2.5) / 5.0, (y - 1.6) / 2.0)
        mesh = _grid_mesh(_VERTICAL_XS, _VERTICAL_YS, uv_of)
        mesh["node"] = "Plane006"
        mesh["kind"] = MeshKind.VERTICAL
        triangles = inline_vertex_meters(mesh, MeshKind.VERTICAL)

        # Corner vertex on a kept column and kept row carries both meters.
        kept = [v for tri in triangles for v in tri
                if abs(v["pos"][0] + 2.5) < 1e-4
                and abs(v["pos"][1] - _VERTICAL_YS[1]) < 1e-4]
        self.assertTrue(kept)
        self.assertEqual(kept[0]["meter"], {"x": 5.0, "y": 0.0})

        # Skipped gridlines: rightmost column (x=2.5) has no meter.x; bottom
        # (y=1.6297) and top (y=3.3797) rows have no meter.y.
        rightmost = [v for tri in triangles for v in tri
                     if abs(v["pos"][0] - 2.5) < 1e-4]
        self.assertTrue(rightmost)
        for vertex in rightmost:
            self.assertNotIn("x", vertex.get("meter", {}))
        bottom_top = [v for tri in triangles for v in tri
                      if abs(v["pos"][1] - 1.6297) < 1e-4
                      or abs(v["pos"][1] - 3.3797) < 1e-4]
        self.assertTrue(bottom_top)
        for vertex in bottom_top:
            self.assertNotIn("y", vertex.get("meter", {}))

    def test_inline_does_not_mutate_source(self):
        mesh = _grid_mesh(_VERTICAL_XS, _VERTICAL_YS)
        mesh["kind"] = MeshKind.VERTICAL
        original = json.dumps(mesh["triangles"])
        inline_vertex_meters(mesh, MeshKind.VERTICAL)
        self.assertEqual(json.dumps(mesh["triangles"]), original)


class MeterLabelTest(unittest.TestCase):
    def test_labels_do_not_mutate_input_and_draw_color(self):
        def uv_of(x, y):
            return ((x + 2.5) / 5.0, (y - 1.6) / 2.0)    # spread across [0,1]
        mesh = _grid_mesh(_VERTICAL_XS, _VERTICAL_YS, uv_of)
        mesh["kind"] = MeshKind.VERTICAL
        grid = grid_annotation(mesh)
        image = np.zeros((720, 1280, 3), dtype=np.uint8)
        original = image.copy()
        result = draw_meter_labels(image, mesh, grid, color=(0, 255, 255))
        np.testing.assert_array_equal(image, original)
        # Both X and Y labels drawn in the SAME mesh color (cyan BGR (0,255,255)).
        self.assertGreater(np.count_nonzero(
            (result[:, :, 1] >= 150) & (result[:, :, 2] >= 150)), 0)

    def test_missing_vertex_is_skipped(self):
        mesh = _grid_mesh(_VERTICAL_XS, _VERTICAL_YS)
        mesh["kind"] = MeshKind.VERTICAL
        # A grid entry for a coordinate that has no vertex in the mesh.
        grid = {"x": [{"x": 9.9, "meter": 0.5}],
                "y": [{"y": 8.8, "meter": 0.25}]}
        image = np.zeros((32, 48, 3), dtype=np.uint8)
        result = draw_meter_labels(image, mesh, grid)
        np.testing.assert_array_equal(image, result)


def _have_camera_assets(camera):
    """True when the water_entry2 sub-camera's FBX + base texture exist."""
    from python.fbx_overlay.profiles import PROFILES
    profile = PROFILES["water_entry2"]
    spec = next(c for c in profile.cameras if c.name == camera)
    texture = spec.fbx.with_suffix(".fbm")
    return spec.fbx.is_file() and (
        (texture / "333.jpg").is_file() if camera == "femto"
        else (texture / "gemini_camera_1_mask_merged.png").is_file())


def _have_overhead_assets(line):
    """True when the canvas line's FBX + .fbm textures exist."""
    from python.fbx_overlay.profiles import get as get_profile
    profile = get_profile(line)
    return profile.fbx.is_file() and profile.tex_dir.is_dir() and any(
        profile.tex_dir.iterdir())


@unittest.skipUnless(
    all(path.is_file() for path, _node, _count in LEGACY_MESHES) and IMAGE.is_file(),
    "local FBX and xlj image assets are not available",
)
class RealAssetTest(unittest.TestCase):
    def test_legacy_nodes_via_mesh_flag(self):
        from python.fbx_overlay.__main__ import _load_mesh

        for path, node_name, triangle_count in LEGACY_MESHES:
            mesh = _load_mesh(path, node_name)
            self.assertEqual(mesh["node"], node_name)
            self.assertEqual(len(mesh["triangles"]), triangle_count)

    def test_legacy_mesh_flag_cli_output(self):
        from python.fbx_overlay.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(main([
                "--output", str(out),
                "--mesh", str(LEGACY_MESHES[0][0]), "Plane004",
                "--mesh", str(LEGACY_MESHES[1][0]), "Plane005",
            ]), 0)
            rendered = cv2.imread(str(out / "Plane004_overlay.png"),
                                  cv2.IMREAD_COLOR)
            self.assertIsNotNone(rendered)
            self.assertEqual(rendered.shape, (720, 1280, 3))
            self.assertGreater(np.count_nonzero(np.all(
                rendered == [0, 255, 255], axis=2)), 0)
            self.assertGreater(np.count_nonzero(np.all(
                rendered == [0, 128, 255], axis=2)), 0)

    def test_water_entry_line_via_profile(self):
        # The legacy 005/006 models are now a real line, not just --mesh.
        from python.fbx_overlay.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(main(["--line", "water_entry",
                                   "--output", str(out)]), 0)
            for camera in ("water_entry_a", "water_entry_b"):
                composite = out / camera / f"{camera}_mesh_overlay.png"
                self.assertTrue(composite.is_file(), composite)
                rendered = cv2.imread(str(composite), cv2.IMREAD_COLOR)
                self.assertIsNotNone(rendered)
                self.assertEqual(rendered.shape, (720, 1280, 3))
                self.assertTrue((out / camera / "mesh.json").is_file())


@unittest.skipUnless(
    all(_have_camera_assets(camera) for camera in CAMERAS),
    "femto/gemini FBX or their .fbm textures are not available",
)
class CameraAssetTest(unittest.TestCase):
    def _load_camera(self, camera):
        from python.fbx_overlay.__main__ import _discover_meshes
        from python.fbx_overlay.profiles import PROFILES
        profile = PROFILES["water_entry2"]
        spec = next(c for c in profile.cameras if c.name == camera)
        return spec, _discover_meshes(spec.fbx)

    def test_nodes_triangles_and_kinds(self):
        for camera, (fbx, expected) in CAMERAS.items():
            spec, meshes = self._load_camera(camera)
            self.assertEqual(spec.fbx, fbx)
            by_node = {mesh["node"]: mesh for mesh in meshes}
            self.assertEqual(set(by_node), {node for node, _t, _k in expected})
            for node, triangles, kind in expected:
                self.assertEqual(len(by_node[node]["triangles"]), triangles)
                self.assertIs(by_node[node]["kind"], kind)

    def test_water_entry2_cli_produces_camera_layout(self):
        from python.fbx_overlay.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(main(["--line", "water_entry2",
                                   "--output", str(out)]), 0)
            for camera in CAMERAS:
                cam_dir = out / camera
                composite = cam_dir / f"{camera}_mesh_overlay.png"
                self.assertTrue(composite.is_file(), composite)
                rendered = cv2.imread(str(composite), cv2.IMREAD_COLOR)
                self.assertIsNotNone(rendered)
                for node, _triangles, kind in CAMERAS[camera][1]:
                    if kind is MeshKind.FULL_FRAME:
                        continue    # the base image; no separate product
                    per_mesh = cam_dir / (
                        f"{camera}_{node}_{kind.value}_overlay.png")
                    self.assertTrue(per_mesh.is_file(), per_mesh)
                    single = cv2.imread(str(per_mesh), cv2.IMREAD_COLOR)
                    self.assertIsNotNone(single)
                    self.assertEqual(single.shape, rendered.shape)

    def test_camera_flag_compat_shim(self):
        from python.fbx_overlay.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            # --camera gemini without --line resolves to water_entry2.
            self.assertEqual(main(["--output", str(out), "--camera", "gemini"]), 0)
            self.assertTrue((out / "gemini" / "gemini_mesh_overlay.png").is_file())
            self.assertFalse((out / "femto").exists())

    def test_mesh_json_layout_and_grid(self):
        from python.fbx_overlay.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(main(["--line", "water_entry2",
                                   "--output", str(out)]), 0)
            for camera in CAMERAS:
                path = out / camera / "mesh.json"
                self.assertTrue(path.is_file(), path)
                doc = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(doc["camera"], "water_entry2")
                self.assertTrue(doc["source"].endswith(f"{camera}.fbx"))
                # No full-frame mesh in the document; every vertex has a meter.
                self.assertEqual({m["kind"] for m in doc["meshes"]},
                                 {"vertical", "surface"})
                for entry in doc["meshes"]:
                    self.assertIsInstance(entry["kind"], str)
                    self.assertNotIn("grid", entry)
                    self.assertTrue(entry["triangles"])
                    any_meter = any("meter" in v for tri in entry["triangles"]
                                    for v in tri)
                    self.assertTrue(any_meter)

    def _vertex_meters(self, mesh_entry):
        """(distinct meter.x values, distinct meter.y values) from vertices."""
        xs, ys = set(), set()
        for triangle in mesh_entry["triangles"]:
            for vertex in triangle:
                meter = vertex.get("meter", {})
                if "x" in meter:
                    xs.add(meter["x"])
                if "y" in meter:
                    ys.add(meter["y"])
        return sorted(xs), sorted(ys)

    def test_mesh_json_matches_measured_fixture(self):
        from python.fbx_overlay.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(main(["--line", "water_entry2",
                                   "--output", str(out)]), 0)
            femto = json.loads((out / "femto" / "mesh.json").read_text(encoding="utf-8"))
            by_node = {m["node"]: m for m in femto["meshes"]}
            self.assertEqual(self._vertex_meters(by_node["Plane006"]),
                             ([0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0],
                              [0.0, 0.25, 0.5, 0.75, 1.0, 1.25]))
            # Three bands, pitch measured at 1.25 m: 0 / 1.25 / 2.5.
            self.assertEqual(self._vertex_meters(by_node["Plane010"]),
                             ([0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0],
                              [0.0, 1.25, 2.5]))

            gemini = json.loads((out / "gemini" / "mesh.json").read_text(encoding="utf-8"))
            by_node = {m["node"]: m for m in gemini["meshes"]}
            self.assertEqual(self._vertex_meters(by_node["Plane007"]),
                             ([0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5],
                              [0.0, 0.25, 0.5, 0.75, 1.0, 1.25]))
            self.assertEqual(self._vertex_meters(by_node["Plane009"]),
                             ([0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0],
                              [0.0, 1.25, 2.5]))

    def test_no_labels_still_writes_json_but_changes_composite(self):
        from python.fbx_overlay.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            labeled_out = base / "labeled"
            plain_out = base / "plain"
            self.assertEqual(main(["--line", "water_entry2",
                                   "--output", str(labeled_out)]), 0)
            self.assertEqual(main(["--line", "water_entry2",
                                   "--output", str(plain_out),
                                   "--no-labels"]), 0)
            self.assertTrue((labeled_out / "femto" / "mesh.json").is_file())
            self.assertTrue((plain_out / "femto" / "mesh.json").is_file())
            labeled = cv2.imread(str(labeled_out / "femto" / "femto_mesh_overlay.png"))
            plain = cv2.imread(str(plain_out / "femto" / "femto_mesh_overlay.png"))
            self.assertGreater(np.count_nonzero(labeled[:, :, 2] >= 120), 0)
            self.assertGreater(
                np.count_nonzero(np.abs(labeled.astype(int) - plain.astype(int))), 0)


def _have_overhead_assets(line):
    from python.fbx_overlay.profiles import get as get_profile
    profile = get_profile(line)
    return profile.fbx.is_file() and profile.tex_dir.is_dir() and any(
        profile.tex_dir.iterdir())


@unittest.skipUnless(
    all(_have_overhead_assets(line) for line in OVERHEAD_LINES),
    "overhead/overhead2 FBX or their .fbm textures are not available",
)
class OverheadAssetTest(unittest.TestCase):
    def test_planes_discover_and_classify(self):
        from python.fbx_overlay.__main__ import _discover_meshes
        from python.fbx_overlay.profiles import get as get_profile
        for line, (fbx, expected) in OVERHEAD_LINES.items():
            profile = get_profile(line)
            self.assertEqual(profile.fbx, fbx)
            meshes = _discover_meshes(profile.fbx)
            by_node = {m["node"]: m for m in meshes}
            self.assertEqual(set(by_node), {node for node, _t in expected})
            for node, triangles in expected:
                self.assertEqual(len(by_node[node]["triangles"]), triangles)
                self.assertIs(by_node[node]["kind"], MeshKind.PLANE)

    def test_cli_overhead_products(self):
        from python.fbx_overlay.__main__ import main
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(main(["--line", "overhead",
                                   "--output", str(out)]), 0)
            self.assertTrue((out / "overhead_mesh_overlay_fbx.png").is_file())
            self.assertTrue(
                (out / "overhead_Plane001_plane_overlay_fbx.png").is_file())
            self.assertTrue(
                (out / "overhead_Plane002_plane_overlay_fbx.png").is_file())
            self.assertTrue((out / "overhead_label_line_compare_fbx.png").is_file())
            # The compare image is the canvas width doubled side by side.
            compare = cv2.imread(str(out / "overhead_label_line_compare_fbx.png"))
            self.assertEqual(compare.shape[1], 4255 * 2)
            doc = json.loads((out / "mesh.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["camera"], "overhead")

    def test_cli_overhead2_products(self):
        from python.fbx_overlay.__main__ import main
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(main(["--line", "overhead2",
                                   "--output", str(out)]), 0)
            self.assertTrue((out / "overhead2_mesh_overlay_fbx.png").is_file())
            self.assertTrue(
                (out / "overhead2_Plane002_plane_overlay_fbx.png").is_file())
            self.assertTrue(
                (out / "overhead2_Plane011_plane_overlay_fbx.png").is_file())
            self.assertTrue(
                (out / "overhead2_label_line_compare_fbx.png").is_file())
            doc = json.loads((out / "mesh.json").read_text(encoding="utf-8"))
            self.assertEqual(doc["camera"], "overhead2")
            by_node = {m["node"]: m for m in doc["meshes"]}
            plane011 = by_node["Plane011"]
            xs = {v["meter"]["x"] for t in plane011["triangles"]
                  for v in t if "x" in v.get("meter", {})}
            ys = {v["meter"]["y"] for t in plane011["triangles"]
                  for v in t if "y" in v.get("meter", {})}
            self.assertAlmostEqual(min(xs), 0.0, places=4)
            self.assertAlmostEqual(max(xs), 17.5, places=4)
            self.assertEqual({round(y, 3) for y in ys},
                             {0.0, 0.875, 1.625, 2.5})

    def test_cli_overhead2_dataset_texture_set(self):
        from python.fbx_overlay.__main__ import main
        from python.fbx_overlay.profiles import get as get_profile
        import tempfile

        dataset_dir = get_profile("overhead2").texture_sets[1][1]
        if not (dataset_dir / "overhead5_merged.png").is_file():
            self.skipTest("dataset texture set not available")
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(main(["--line", "overhead2",
                                   "--texture-set", "dataset",
                                   "--output", str(out)]), 0)
            self.assertTrue(
                (out / "overhead2_mesh_overlay_dataset.png").is_file())

    def test_camera_overhead_compat_shim(self):
        from python.fbx_overlay.__main__ import main
        import tempfile

        # Bare --camera overhead (no --line) maps to the overhead line.
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory)
            self.assertEqual(main(["--camera", "overhead",
                                   "--output", str(out)]), 0)
            self.assertTrue((out / "overhead_mesh_overlay_fbx.png").is_file())


if __name__ == "__main__":
    unittest.main()
