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

    def test_camera_of_parses_texture_basename(self):
        from python.stitch.render_video import camera_of

        self.assertEqual(camera_of("underA7-grid.png"), "underA7")
        self.assertEqual(camera_of("underA16-grid.png"), "underA16")
        self.assertIsNone(camera_of("pool.png"))
        self.assertIsNone(camera_of(None))

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
