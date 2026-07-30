import unittest
from pathlib import Path

from python.stitch.extract import sort_meshes_by_world_x, select_pool_planes

try:
    import fbx  # noqa: F401
    HAS_FBX = True
except Exception:
    HAS_FBX = False

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_01D = PROJECT_ROOT / "inputs" / "underwater" / "models" / "01d.fbx"
TEXDIR_01D = PROJECT_ROOT / "inputs" / "underwater" / "models" / "01d.fbm"


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


def _band_plane(node, tex, x0, tris=64):
    # a full-height pool plane: world-Y (pos[1]) inside the pool band, height 3
    tri = [
        {"pos": [x0, -11.28], "uv": [0.0, 0.0]},
        {"pos": [x0 + 5.5, -11.28], "uv": [1.0, 0.0]},
        {"pos": [x0, -8.28], "uv": [0.0, 1.0]},
    ]
    return {"node": node, "texture_basename": tex, "triangles": [tri] * tris}


def _strip(node, tex, x0):
    # a short lane-marker strip near world-Y 0 (height ~0.5), should be dropped
    tri = [
        {"pos": [x0, -0.24], "uv": [0.0, 0.0]},
        {"pos": [x0 + 0.85, -0.24], "uv": [1.0, 0.0]},
        {"pos": [x0, 0.24], "uv": [0.0, 1.0]},
    ]
    return {"node": node, "texture_basename": tex, "triangles": [tri]}


def _plane(node, tex, x0, width=1.0, y0=0.0, y1=1.0):
    """One quad, two triangles, UV spanning the full texture."""
    corners = [
        [{"pos": [x0, y0], "uv": [0.0, 0.0]},
         {"pos": [x0 + width, y0], "uv": [1.0, 0.0]},
         {"pos": [x0 + width, y1], "uv": [1.0, 1.0]}],
        [{"pos": [x0, y0], "uv": [0.0, 0.0]},
         {"pos": [x0 + width, y1], "uv": [1.0, 1.0]},
         {"pos": [x0, y1], "uv": [0.0, 1.0]}],
    ]
    return {"node": node, "texture_basename": tex, "uvset": "UVChannel_1",
            "const_axis": 2, "kept_axes": [0, 1], "spans": [width, y1 - y0, 0],
            "triangles": corners}


def _two_plane_json(td, planes):
    """Write a mesh JSON of `planes` (already built by _plane) and return it."""
    import json

    path = Path(td) / "mesh.json"
    path.write_text(json.dumps({"source": "test", "meshes": list(planes)}))
    return path


class SelectPoolPlanesTest(unittest.TestCase):
    def test_keeps_one_full_height_plane_per_texture(self):
        meshes = [
            _band_plane("planeA", "a.png", 0.0),
            _strip("stripA", "a.png", 0.0),
            _band_plane("planeB", "b.png", 5.0),
            {"node": "frame", "texture_basename": None, "triangles": [
                [{"pos": [0.0, 0.0], "uv": [0, 0]},
                 {"pos": [1.0, 0.0], "uv": [1, 0]},
                 {"pos": [0.0, 1.0], "uv": [0, 1]}]]},
        ]
        kept = select_pool_planes(meshes)
        self.assertEqual(
            sorted(m["node"] for m in kept), ["planeA", "planeB"])

    def test_prefers_highest_tri_count_among_duplicates(self):
        meshes = [
            _band_plane("small", "a.png", 0.0, tris=10),
            _band_plane("big", "a.png", 0.0, tris=200),
        ]
        kept = select_pool_planes(meshes)
        self.assertEqual([m["node"] for m in kept], ["big"])

    def test_drops_untextured_and_strips(self):
        meshes = [_strip("s", "a.png", 0.0)]
        self.assertEqual(select_pool_planes(meshes), [])


class VideoAlignmentTest(unittest.TestCase):
    """Time alignment must come from the manifest wall clocks, not file order."""

    def test_start_frames_follow_playback_formula(self):
        from python.stitch.render_video import alignment_plan

        align_start, align_end, fps = 1_000_000, 1_030_000, 30.0
        cams = {
            # frame 0 lands 2970ms before align_start -> start at frame 89
            "underA2": {"keyframe_ms": align_start - 2970,
                        "last_decodable_ms": align_end, "frames": 989},
            # frame 0 lands 14ms before align_start -> start at frame 0
            "underA1": {"keyframe_ms": align_start - 14,
                        "last_decodable_ms": align_end, "frames": 900},
        }
        starts, report = alignment_plan(
            align_start, align_end, fps, cams, ["underA2", "underA1"])

        self.assertEqual(starts, [89, 0])
        self.assertEqual([r["skew_ms"] for r in report], [2970, 14])
        self.assertFalse(any(r["late_start"] for r in report))

    def test_flags_camera_starting_after_align_start(self):
        from python.stitch.render_video import alignment_plan

        align_start, align_end, fps = 1_000_000, 1_030_000, 30.0
        cams = {"underA1": {"keyframe_ms": align_start + 500,
                            "last_decodable_ms": align_end, "frames": 900}}
        starts, report = alignment_plan(
            align_start, align_end, fps, cams, ["underA1"])

        self.assertEqual(starts, [0])          # clamped, cannot read before frame 0
        self.assertTrue(report[0]["late_start"])

    def test_reports_short_tail_against_align_end(self):
        from python.stitch.render_video import alignment_plan

        align_start, align_end, fps = 1_000_000, 1_030_000, 30.0
        cams = {"underA1": {"keyframe_ms": align_start,
                            "last_decodable_ms": align_end - 400, "frames": 890}}
        _starts, report = alignment_plan(
            align_start, align_end, fps, cams, ["underA1"])

        self.assertEqual(report[0]["short_ms"], 400)

    def test_manifest_without_align_window_is_fatal(self):
        import json
        import tempfile
        from python.stitch.render_video import load_manifest

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "manifest.json").write_text(json.dumps({"files": []}))
            with self.assertRaises(SystemExit):
                load_manifest(td)

    def test_missing_manifest_is_fatal(self):
        import tempfile
        from python.stitch.render_video import load_manifest

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(SystemExit):
                load_manifest(Path(td))

    def test_falls_back_to_first_decodable_anchor(self):
        import json
        import tempfile
        from python.stitch.render_video import load_manifest

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "manifest.json").write_text(json.dumps({
                "align_start_ms": 10, "align_end_ms": 20, "fps": 30.0,
                "files": [{"source_id": "underA1",
                           "first_decodable_timestamp_ms": 7,
                           "last_decodable_timestamp_ms": 20, "frames": 30}],
            }))
            start, end, fps, cams = load_manifest(td)

            self.assertEqual((start, end, fps), (10, 20, 30.0))
            self.assertEqual(cams["underA1"]["keyframe_ms"], 7)


class ExtractIntegrationTest(unittest.TestCase):
    @unittest.skipUnless(HAS_FBX, "FBX SDK not available")
    @unittest.skipUnless(MODEL_01D.is_file(), "01d.fbx not present")
    def test_extracts_two_ordered_meshes(self):
        import tempfile
        from python.stitch.extract import extract_to_json

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
        from python.stitch.render import resolve_ppm
        # world X span 2.0 -> ppm ~ 320 for 640 target
        self.assertAlmostEqual(resolve_ppm(-1.0, 1.0, 640), 320.0, places=3)

    def test_resolve_ppm_degenerate_span_falls_back(self):
        from python.stitch.render import resolve_ppm
        self.assertEqual(resolve_ppm(0.0, 0.0, 640), 100.0)

    def test_render_writes_still_and_grid(self):
        import json
        import tempfile
        import cv2
        import numpy as np
        from python.stitch.render import render_stills

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
        from python.stitch.render import render_stills

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
        from python.stitch.render import crop_bottom_and_scale

        image = np.zeros((100, 200, 3), np.uint8)
        image[:80] = (10, 20, 30)
        image[80:] = (200, 210, 220)

        result = crop_bottom_and_scale(image, crop_px=20, target_height=100)

        self.assertEqual(result.shape, (100, 250, 3))
        self.assertLess(int(result[..., 0].max()), 100)

    def test_bottom_dirty_rows_counts_ragged_tail(self):
        import numpy as np
        from python.stitch.render import bottom_dirty_rows

        # 10 rows: rows 0..6 fully covered (width 5), rows 7..9 ragged
        cov = np.zeros((10, 5), np.uint8)
        cov[:7] = 1
        cov[7, :3] = 1
        cov[8, :1] = 1
        # row 9 all zero
        self.assertEqual(bottom_dirty_rows(cov), 3)

    def test_bottom_dirty_rows_zero_when_clean(self):
        import numpy as np
        from python.stitch.render import bottom_dirty_rows

        cov = np.ones((8, 5), np.uint8)
        self.assertEqual(bottom_dirty_rows(cov), 0)

    def test_full_res_crop_bottom_restores_source_height_and_scales_width(self):
        import json
        import tempfile
        import cv2
        import numpy as np
        from python.stitch.render import render_stills

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
        from python.stitch.render import render_stills

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            bad_path = td / "bad.json"
            bad_path.write_text(json.dumps({"source": "x"}))
            still = td / "out_stitch.png"
            grid = td / "out_grid.png"
            with self.assertRaises(SystemExit):
                render_stills(bad_path, td, still, grid)


class OneClickRunnerTest(unittest.TestCase):
    """The one-command runner must pick the right platform toolchain and emit a
    config whose lane order matches the compiled asset."""

    def test_platform_selects_backend_build_dir_and_executable(self):
        import python.stitch.run as runner

        original = runner.platform.system
        try:
            runner.platform.system = lambda: "Darwin"
            self.assertEqual(runner.default_backend(), "metal")
            self.assertEqual(runner.build_dir_for("metal").name, "metal-release")
            self.assertEqual(
                runner.executable_for(runner.build_dir_for("metal")).name,
                "swim_realtime")

            runner.platform.system = lambda: "Windows"
            self.assertEqual(runner.default_backend(), "d3d11")
            self.assertEqual(runner.build_dir_for("d3d11").name, "win-d3d11")
            self.assertEqual(
                runner.executable_for(runner.build_dir_for("d3d11")).name,
                "swim_realtime.exe")
        finally:
            runner.platform.system = original

    def test_generated_config_declares_sixteen_lanes_right_to_left(self):
        import tempfile
        import python.stitch.run as runner

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for index in range(1, 17):
                (td / f"swb_test_underA{index}.ts").write_bytes(b"")
            config = td / "generated.conf"
            runner.write_config(config, td, "metal", td / "out.h265")

            lines = config.read_text().splitlines()
            sources = [line.split("=", 1)[0].removeprefix("source.")
                       for line in lines if line.startswith("source.")]
            # extract orders meshes left-to-right, which is underA16 -> underA1
            self.assertEqual(sources, [f"underA{i}" for i in range(16, 0, -1)])
            self.assertIn("backend=metal", lines)

    def test_missing_clip_is_reported_not_silently_skipped(self):
        import tempfile
        import python.stitch.run as runner

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for index in range(1, 16):          # underA16 absent
                (td / f"swb_test_underA{index}.ts").write_bytes(b"")
            with self.assertRaises(runner.StepError):
                runner.write_config(td / "c.conf", td, "metal", td / "o.h265")

    def test_newer_than_treats_missing_target_as_stale(self):
        import tempfile
        import python.stitch.run as runner

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            source = td / "src"
            source.write_text("x")
            self.assertFalse(runner.newer_than(td / "absent", source))

            target = td / "target"
            target.write_text("y")
            self.assertTrue(runner.newer_than(target, source))


class AssetShapingTest(unittest.TestCase):
    """clip_uv and crop_bottom must reproduce the offline renderer's geometry
    while leaving the pool defaults byte-identical."""

    def _tiny_mesh(self, td):
        """A two-plane mesh whose UVs run past the source image, so clipping and
        the ragged bottom both have something to act on."""
        import json

        def plane(node, x0, uv_lo, uv_hi):
            quad = [
                [{"pos": [x0, 0.0], "uv": [uv_lo, uv_lo]},
                 {"pos": [x0 + 1.0, 0.0], "uv": [uv_hi, uv_lo]},
                 {"pos": [x0 + 1.0, 1.0], "uv": [uv_hi, uv_hi]}],
                [{"pos": [x0, 0.0], "uv": [uv_lo, uv_lo]},
                 {"pos": [x0 + 1.0, 1.0], "uv": [uv_hi, uv_hi]},
                 {"pos": [x0, 1.0], "uv": [uv_lo, uv_hi]}],
            ]
            return {"node": node, "texture_basename": f"{node}.png",
                    "uvset": "map1", "const_axis": 2, "kept_axes": [0, 1],
                    "spans": [1, 1, 0], "triangles": quad}

        path = td / "mesh.json"
        path.write_text(json.dumps({"source": "x", "meshes": [
            plane("left", 0.0, -0.2, 0.9),
            plane("right", 0.8, 0.1, 1.2),
        ]}))
        return path

    def test_clip_uv_shrinks_coverage_without_moving_geometry(self):
        import tempfile
        from python.assets.compile_runtime_asset import compile_asset

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mesh = self._tiny_mesh(td)
            plain = compile_asset(mesh, td / "plain.swasset", ["a", "b"], 64.0,
                                  neg_v=False, blend_px=0.0, clip_uv=False)
            clipped = compile_asset(mesh, td / "clip.swasset", ["a", "b"], 64.0,
                                    neg_v=False, blend_px=0.0, clip_uv=True,
                                    source_size=(32, 32))
            # clipping only removes coverage; the canvas is unchanged
            self.assertEqual(plain["logical_width"], clipped["logical_width"])
            self.assertEqual(plain["logical_height"], clipped["logical_height"])
            self.assertLess((td / "clip.swasset").stat().st_size,
                            (td / "plain.swasset").stat().st_size)

    def test_crop_bottom_shortens_the_canvas(self):
        import tempfile
        from python.assets.compile_runtime_asset import compile_asset

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mesh = self._tiny_mesh(td)
            full = compile_asset(mesh, td / "full.swasset", ["a", "b"], 64.0,
                                 neg_v=False, blend_px=0.0, crop_bottom="none")
            cropped = compile_asset(mesh, td / "crop.swasset", ["a", "b"], 64.0,
                                    neg_v=False, blend_px=0.0, crop_bottom=8)
            self.assertEqual(cropped["crop_rows"], 8)
            self.assertEqual(cropped["logical_height"],
                             full["logical_height"] - 8)
            self.assertEqual(cropped["canvas_height"], full["canvas_height"])
            # encoded stays the logical size rounded up to even
            self.assertEqual(cropped["encoded_height"],
                             cropped["logical_height"] +
                             (cropped["logical_height"] & 1))

    def test_crop_bottom_rejects_removing_the_whole_canvas(self):
        import tempfile
        from python.assets.compile_runtime_asset import compile_asset

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            mesh = self._tiny_mesh(td)
            with self.assertRaises(ValueError):
                compile_asset(mesh, td / "x.swasset", ["a", "b"], 64.0,
                              neg_v=False, crop_bottom=100000)

    def test_defaults_keep_the_pool_bake_unchanged(self):
        # The pool asset predates these options; its defaults must stay off so
        # the committed 5002x2102 geometry is reproduced exactly.
        import inspect
        from python.assets.compile_runtime_asset import compile_asset

        defaults = inspect.signature(compile_asset).parameters
        self.assertIs(defaults["clip_uv"].default, False)
        self.assertIsNone(defaults["crop_bottom"].default)
        self.assertIs(defaults["neg_v"].default, True)


class LaneAlignmentConfigTest(unittest.TestCase):
    def test_start_offsets_come_from_the_manifest_skew(self):
        import python.stitch.run as runner

        # alignment_plan reports skew per lane; run.py turns each into ms and
        # clamps lanes that begin after align_start to zero.
        align_start, align_end, fps = 1_000_000, 1_012_000, 30.0
        cams = {
            "underA16": {"keyframe_ms": align_start - 3083,
                         "last_decodable_ms": align_end, "frames": 400},
            "underA1": {"keyframe_ms": align_start + 250,
                        "last_decodable_ms": align_end, "frames": 400},
        }
        order = ["underA16", "underA1"]
        starts, report = runner.RV.alignment_plan(
            align_start, align_end, fps, cams, order)
        offsets = {entry["cam"]: max(0, entry["skew_ms"]) for entry in report}

        self.assertEqual(offsets["underA16"], 3083)
        self.assertEqual(offsets["underA1"], 0)      # starts after align_start
        self.assertTrue(report[1]["late_start"])

    def test_config_omits_start_ms_when_alignment_is_disabled(self):
        import tempfile
        import python.stitch.run as runner

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for index in range(1, 17):
                (td / f"swb_test_underA{index}.ts").write_bytes(b"")
            config = td / "c.conf"
            runner.write_config(config, td, "metal", td / "o.h265", align=False)
            self.assertNotIn("start_ms", config.read_text())


class ProfileTest(unittest.TestCase):
    """A profile is the single place a stitch line's differences live."""

    def test_registry_holds_both_lines(self):
        from python.stitch import profiles

        self.assertEqual(profiles.names(), ["underwater", "overhead"])

    def test_underwater_values_match_the_shipped_pipeline(self):
        # These are the numbers the committed underwater artefacts were made
        # with; a profile that drifts from them silently changes the bake.
        from python.stitch import profiles

        p = profiles.get("underwater")
        self.assertEqual(p.camera_ids, tuple(f"underA{i}" for i in range(16, 0, -1)))
        self.assertEqual(p.clip_suffix, ".ts")
        self.assertEqual(p.ppm, 240.0)
        self.assertEqual(p.blend_px, 120.0)
        self.assertTrue(p.full_res)
        self.assertEqual(p.crop_bottom, "auto")
        self.assertTrue(p.clip_uv)
        self.assertTrue(p.planes_only)
        self.assertEqual(p.sync, "manifest")
        self.assertEqual(p.source_size, (1280, 720))
        self.assertEqual(p.ref_tex, "snapshot")
        self.assertEqual(p.asset.name, "underwater.swasset")

    def test_overhead_values_match_the_design(self):
        from python.stitch import profiles

        p = profiles.get("overhead")
        self.assertEqual(p.camera_ids, ("overhead5", "overhead6"))
        self.assertEqual(p.clip_suffix, ".ts")
        self.assertEqual(p.ppm, 170.0)
        self.assertEqual(p.blend_px, 85.0)
        self.assertFalse(p.full_res)
        self.assertEqual(p.crop_bottom, "none")
        self.assertTrue(p.clip_uv)
        self.assertFalse(p.planes_only)
        self.assertEqual(p.sync, "manifest")
        self.assertEqual(p.source_size, (3840, 2160))
        self.assertEqual(p.ref_tex, "video")
        self.assertEqual(p.fbx.name, "002.fbx")
        self.assertEqual(p.asset.name, "overhead.swasset")

    def test_unknown_name_lists_the_registered_ones(self):
        from python.stitch import profiles

        with self.assertRaises(SystemExit) as caught:
            profiles.get("pool")
        message = str(caught.exception)
        self.assertIn("pool", message)
        self.assertIn("underwater", message)
        self.assertIn("overhead", message)

    def test_profile_is_immutable(self):
        import dataclasses
        from python.stitch import profiles

        with self.assertRaises(dataclasses.FrozenInstanceError):
            profiles.get("overhead").ppm = 1.0

    def test_overhead_still_tex_dir_is_the_designer_fbm(self):
        # underwater renders stills from the dataset's annotation-grids, not the
        # grids baked into the .fbm; overhead has no such split.
        from python.stitch import profiles

        p = profiles.get("overhead")
        self.assertEqual(p.still_tex_dir, p.tex_dir)
        self.assertEqual(p.tex_dir.name, "002.fbm")

    def test_grid_dir_honours_the_explicit_override(self):
        import os
        from unittest.mock import patch
        from python.stitch import profiles

        with patch.dict(os.environ, {"STITCH_GRID_DIR": "/tmp/grids-xyz"}):
            self.assertEqual(str(profiles.grid_dir()), "/tmp/grids-xyz")

    def test_grid_dir_falls_back_to_the_dataset_root(self):
        import os
        from unittest.mock import patch
        from python.stitch import profiles

        env = {"ANNOTATION_PREVIEW_DATASET_ROOT": "/tmp/ds-xyz"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("STITCH_GRID_DIR", None)
            self.assertEqual(str(profiles.grid_dir()),
                             "/tmp/ds-xyz/annotation-grids")

    def test_every_profile_has_a_distinct_out_dir_and_asset(self):
        from python.stitch import profiles

        all_profiles = [profiles.get(name) for name in profiles.names()]
        out_dirs = [p.out_dir for p in all_profiles]
        assets = [p.asset for p in all_profiles]
        self.assertEqual(len(set(out_dirs)), len(out_dirs))
        self.assertEqual(len(set(assets)), len(assets))

    def test_clip_for_matches_the_profile_suffix(self):
        import tempfile
        from python.stitch import profiles

        overhead = profiles.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "swb_x_overhead5.ts").write_bytes(b"")
            (td / "swb_x_overhead5.mp4").write_bytes(b"")   # wrong suffix
            found = overhead.clip_for(td, "overhead5")
            self.assertEqual(found.name, "swb_x_overhead5.ts")

    def test_clip_for_reports_a_missing_clip(self):
        import tempfile
        from python.stitch import profiles

        overhead = profiles.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(profiles.StepError):
                overhead.clip_for(Path(td), "overhead5")

    def test_clip_for_refuses_to_guess_between_two_matches(self):
        import tempfile
        from python.stitch import profiles

        overhead = profiles.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "a_overhead5.ts").write_bytes(b"")
            (td / "b_overhead5.ts").write_bytes(b"")
            with self.assertRaises(profiles.StepError):
                overhead.clip_for(td, "overhead5")


class LoopPeriodTest(unittest.TestCase):
    """Every lane must wrap on the same content period, or they drift apart by
    the difference in their usable spans on every pass."""

    def test_period_is_the_shortest_usable_span(self):
        import python.stitch.run as runner

        # spans after the aligned start: 900, 950, 880 -> the shortest wins
        cams = {
            "underA3": {"keyframe_ms": 0, "last_decodable_ms": 1200},
            "underA2": {"keyframe_ms": 0, "last_decodable_ms": 1150},
            "underA1": {"keyframe_ms": 0, "last_decodable_ms": 1080},
        }
        offsets = {"underA3": 300, "underA2": 200, "underA1": 200}
        original = runner.RV.load_manifest
        try:
            runner.RV.load_manifest = lambda _d: (0, 1000, 30.0, cams)
            self.assertEqual(runner.loop_period_ms("ignored", offsets), 880)
        finally:
            runner.RV.load_manifest = original

    def test_period_is_zero_without_a_manifest(self):
        import python.stitch.run as runner

        original = runner.RV.load_manifest
        try:
            def missing(_d):
                raise SystemExit("no manifest")
            runner.RV.load_manifest = missing
            # zero tells the runtime to use each file's own end
            self.assertEqual(runner.loop_period_ms("ignored", {}), 0)
        finally:
            runner.RV.load_manifest = original

    def test_config_carries_loop_controls_only_when_requested(self):
        import tempfile
        import python.stitch.run as runner

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for index in range(1, 17):
                (td / f"swb_test_underA{index}.ts").write_bytes(b"")
            # looping is the default; --no-loop ends the run at EOF instead
            on = td / "on.conf"
            runner.write_config(on, td, "metal", td / "o.h265", align=False)
            self.assertIn("loop_sources=true", on.read_text())
            self.assertIn("stop_at_eof=false", on.read_text())

            off = td / "off.conf"
            runner.write_config(off, td, "metal", td / "o.h265", align=False,
                                loop=False)
            self.assertIn("loop_sources=false", off.read_text())
            self.assertIn("stop_at_eof=true", off.read_text())


class VideoCameraOrderTest(unittest.TestCase):
    """Camera identity comes from the profile's ordered ids, not from parsing a
    texture filename: the overhead textures are 05-02.jpg and C06.jpg, which no
    naming rule maps to overhead5/overhead6."""

    def test_camera_count_must_match_mesh_count(self):
        import tempfile
        from python.stitch.render_video import render_video

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            data = _two_plane_json(td, [_plane("a", "05-02.jpg", 0.0),
                                        _plane("b", "C06.jpg", 0.9)])
            with self.assertRaises(SystemExit) as caught:
                render_video(data, td, td / "out.mp4",
                             camera_ids=("overhead5",),          # one id, two meshes
                             clip_for=lambda d, c: td / "absent.mp4")
            message = str(caught.exception)
            self.assertIn("1", message)
            self.assertIn("2", message)

    def test_clip_lookup_is_delegated_to_the_caller(self):
        # render_video must not glob for clips itself; the profile owns the
        # suffix and the ambiguity rules.
        import tempfile
        from python.stitch.render_video import render_video

        asked = []

        def fake_clip_for(video_dir, camera):
            asked.append(camera)
            raise RuntimeError("stop here")

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            data = _two_plane_json(td, [_plane("a", "05-02.jpg", 0.0),
                                        _plane("b", "C06.jpg", 0.9)])
            with self.assertRaises(RuntimeError):
                render_video(data, td, td / "out.mp4",
                             camera_ids=("overhead5", "overhead6"),
                             clip_for=fake_clip_for)
        self.assertEqual(asked, ["overhead5"])

    def test_camera_of_is_gone(self):
        # The underA-only regex was the last thing tying the video path to the
        # underwater naming scheme.
        import python.stitch.render_video as rv

        self.assertFalse(hasattr(rv, "camera_of"))
        self.assertFalse(hasattr(rv, "video_for_camera"))


class RefTexTest(unittest.TestCase):
    """Reference textures are named after the camera, not after the mesh's
    texture basename: the overhead basenames (05-02.jpg, C06.jpg) say nothing
    about which camera they came from, and reusing a .jpg name would re-encode
    a lossless frame as JPEG."""

    def test_tex_names_follow_camera_ids(self):
        from python.stitch import export_ref_tex, profiles

        self.assertEqual(export_ref_tex.tex_names(profiles.get("overhead")),
                         ["overhead5.png", "overhead6.png"])
        names = export_ref_tex.tex_names(profiles.get("underwater"))
        self.assertEqual(names[0], "underA16.png")
        self.assertEqual(names[-1], "underA1.png")
        self.assertEqual(len(names), 16)

    def test_video_source_requires_a_video_dir(self):
        from python.stitch import export_ref_tex, profiles

        with self.assertRaises(profiles.StepError):
            export_ref_tex.export(profiles.get("overhead"), video_dir=None)

    def test_video_source_writes_one_png_per_camera(self):
        import tempfile
        import cv2
        import numpy as np
        from python.stitch import export_ref_tex, profiles

        overhead = profiles.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            clips = td / "clips"
            clips.mkdir()
            # two one-frame mp4s, distinguishable by colour
            for index, camera in enumerate(overhead.camera_ids):
                frame = np.full((16, 32, 3), 40 * (index + 1), np.uint8)
                writer = cv2.VideoWriter(
                    str(clips / f"sess_{camera}.ts"),
                    cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (32, 16))
                writer.write(frame)
                writer.release()

            out = td / "ref_tex"
            written = export_ref_tex.export(overhead, out_dir=out, video_dir=clips)

            self.assertEqual([p.name for p in written], ["overhead5.png", "overhead6.png"])
            for path in written:
                self.assertTrue(path.is_file())
                self.assertEqual(cv2.imread(str(path)).shape, (16, 32, 3))

    def test_unreadable_clip_is_reported(self):
        # OpenCV prints "moov atom not found" to stderr here; the point is that
        # export raises instead of writing a black frame.
        import tempfile
        from python.stitch import export_ref_tex, profiles

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for camera in profiles.get("overhead").camera_ids:
                (td / f"sess_{camera}.ts").write_bytes(b"not a video")
            with self.assertRaises(profiles.StepError):
                export_ref_tex.export(profiles.get("overhead"),
                                      out_dir=td / "out", video_dir=td)


class RenderTexNamesTest(unittest.TestCase):
    """render_stills reads texture_basename by default and positional names when
    asked, so one renderer serves both the designer's calibration frames and the
    camera-named reference exports."""

    def test_positional_names_render_the_same_as_basenames(self):
        import tempfile
        import cv2
        import numpy as np
        from python.stitch.render import render_stills

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            data = _two_plane_json(td, [_plane("a", "05-02.jpg", 0.0),
                                        _plane("b", "C06.jpg", 0.9)])
            left = np.full((16, 32, 3), 90, np.uint8)
            right = np.full((16, 32, 3), 180, np.uint8)
            # same pixels under both naming schemes, both lossless
            cv2.imwrite(str(td / "05-02.jpg"), left)
            cv2.imwrite(str(td / "C06.jpg"), right)
            cv2.imwrite(str(td / "overhead5.png"), left)
            cv2.imwrite(str(td / "overhead6.png"), right)

            by_basename = td / "a.png"
            by_position = td / "b.png"
            render_stills(data, td, by_basename, None, ppm=64.0)
            render_stills(data, td, by_position, None, ppm=64.0,
                          tex_names=["overhead5.png", "overhead6.png"])

            self.assertTrue(np.array_equal(cv2.imread(str(by_basename)),
                                           cv2.imread(str(by_position))))

    def test_tex_names_length_must_match_mesh_count(self):
        import tempfile
        from python.stitch.render import render_stills

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            data = _two_plane_json(td, [_plane("a", "05-02.jpg", 0.0),
                                        _plane("b", "C06.jpg", 0.9)])
            with self.assertRaises(SystemExit) as caught:
                render_stills(data, td, td / "out.png", None, ppm=64.0,
                              tex_names=["overhead5.png"])
            self.assertIn("1", str(caught.exception))
            self.assertIn("2", str(caught.exception))
