import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from python.labeling import snapshots as S
from python.labeling.merge_overhead import (
    BAND_ROWS,
    CAMERAS,
    FrameSizeError,
    load_stack,
    main,
    median_background,
    merge_frames,
    run_camera,
)


def _solid(h, w, rgb):
    """构造一张纯色帧。"""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :] = rgb
    return frame


class ConstantsTest(unittest.TestCase):
    def test_camera_ids(self):
        self.assertEqual(CAMERAS, ("overhead5", "overhead6", "orbbec_camera_1"))

    def test_default_band_rows(self):
        self.assertEqual(BAND_ROWS, 256)


class MedianBackgroundTest(unittest.TestCase):
    def test_takes_per_pixel_median_over_time(self):
        stack = np.stack([
            _solid(4, 4, (10, 10, 10)),
            _solid(4, 4, (200, 200, 200)),
            _solid(4, 4, (50, 50, 50)),
        ])
        bg = median_background(stack, band_rows=BAND_ROWS)
        self.assertEqual(bg.dtype, np.uint8)
        self.assertTrue((bg == 50).all())

    def test_band_split_matches_single_pass(self):
        rng = np.random.default_rng(7)
        stack = rng.integers(0, 256, size=(5, 13, 11, 3), dtype=np.uint8)
        whole = median_background(stack, band_rows=10_000)
        for band_rows in (1, 7, 13, 10_000):
            with self.subTest(band_rows=band_rows):
                np.testing.assert_array_equal(
                    median_background(stack, band_rows=band_rows), whole)


class MergeFramesTest(unittest.TestCase):
    def test_latest_above_threshold_frame_wins(self):
        bg = _solid(2, 2, (0, 0, 0))
        early = _solid(2, 2, (100, 0, 0))
        late = _solid(2, 2, (200, 0, 0))
        merged = merge_frames(np.stack([early, late]), bg, thresh=40)
        self.assertTrue((merged[:, :, 0] == 200).all())

    def test_below_threshold_pixels_keep_background(self):
        bg = _solid(2, 2, (100, 100, 100))
        quiet = _solid(2, 2, (110, 100, 100))          # 距离 10 < 40
        merged = merge_frames(np.stack([quiet]), bg, thresh=40)
        np.testing.assert_array_equal(merged, bg)

    def test_keeps_source_shape_and_dtype(self):
        bg = _solid(5, 7, (0, 0, 0))
        stack = np.stack([_solid(5, 7, (200, 0, 0))])
        merged = merge_frames(stack, bg, thresh=40)
        self.assertEqual(merged.shape, (5, 7, 3))
        self.assertEqual(merged.dtype, np.uint8)

    def test_band_split_matches_single_pass(self):
        rng = np.random.default_rng(11)
        stack = rng.integers(0, 256, size=(4, 13, 11, 3), dtype=np.uint8)
        bg = median_background(stack, band_rows=10_000)
        whole = merge_frames(stack, bg, band_rows=10_000)
        for band_rows in (1, 5, 13, 10_000):
            with self.subTest(band_rows=band_rows):
                np.testing.assert_array_equal(
                    merge_frames(stack, bg, band_rows=band_rows), whole)

    def test_does_not_mutate_inputs(self):
        bg = _solid(3, 3, (0, 0, 0))
        stack = np.stack([_solid(3, 3, (200, 0, 0))])
        before_bg, before_stack = bg.copy(), stack.copy()
        merge_frames(stack, bg, thresh=40)
        np.testing.assert_array_equal(bg, before_bg)
        np.testing.assert_array_equal(stack, before_stack)


class LoadStackTest(unittest.TestCase):
    def test_stacks_frames_in_given_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i, value in enumerate((10, 200)):
                path = os.path.join(tmp, "f%d.png" % i)
                Image.fromarray(_solid(6, 8, (value, value, value))).save(path)
                paths.append(path)
            stack = load_stack(paths)
            self.assertEqual(stack.shape, (2, 6, 8, 3))
            self.assertEqual(stack.dtype, np.uint8)
            self.assertTrue((stack[0] == 10).all())
            self.assertTrue((stack[1] == 200).all())

    def test_keeps_source_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.png")
            Image.fromarray(_solid(8, 12, (30, 30, 30))).save(path)
            stack = load_stack([path])
            self.assertEqual(stack.shape, (1, 8, 12, 3))

    def test_rejects_mismatched_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = os.path.join(tmp, "a.png")
            second = os.path.join(tmp, "b.png")
            Image.fromarray(_solid(6, 8, (10, 10, 10))).save(first)
            Image.fromarray(_solid(7, 8, (10, 10, 10))).save(second)
            with self.assertRaises(FrameSizeError) as ctx:
                load_stack([first, second])
            self.assertIn("b.png", str(ctx.exception))


def _write_snapshot(snap_dir, snapshot_id, cam, frame):
    """按真实命名写一张快照图：<snap>/<id>/9_x__<cam>.jpg。"""
    d = os.path.join(snap_dir, snapshot_id)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "9_x__%s.jpg" % cam)
    Image.fromarray(frame).save(path, quality=100)
    return path


class RunCameraTest(unittest.TestCase):
    def test_writes_background_and_merged_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            out_dir = os.path.join(tmp, "out")
            quiet = _solid(24, 32, (60, 60, 60))
            loud = quiet.copy()
            loud[4:8, 4:8] = (250, 10, 10)
            for i, frame in enumerate((quiet, quiet, loud)):
                _write_snapshot(snap_dir, "raw_178348017357%d_%d" % (i, i + 1),
                                "overhead5", frame)
            with patch.object(S, "SNAPSHOTS", Path(snap_dir)):
                written = run_camera("overhead5", out_dir=out_dir)
            names = sorted(os.path.basename(p) for p in written)
            self.assertEqual(names, ["overhead5_background.png",
                                     "overhead5_merged.png"])
            for path in written:
                self.assertTrue(os.path.exists(path), path)

    def test_writes_no_labeled_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            out_dir = os.path.join(tmp, "out")
            for i in range(3):
                _write_snapshot(snap_dir, "raw_178348017357%d_%d" % (i, i + 1),
                                "overhead5", _solid(24, 32, (60, 60, 60)))
            with patch.object(S, "SNAPSHOTS", Path(snap_dir)):
                run_camera("overhead5", out_dir=out_dir)
            self.assertEqual(sorted(os.listdir(out_dir)),
                             ["overhead5_background.png", "overhead5_merged.png"])

    def test_products_keep_source_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            out_dir = os.path.join(tmp, "out")
            for i in range(3):
                _write_snapshot(snap_dir, "raw_178348017357%d_%d" % (i, i + 1),
                                "overhead5", _solid(24, 32, (60, 60, 60)))
            with patch.object(S, "SNAPSHOTS", Path(snap_dir)):
                written = run_camera("overhead5", out_dir=out_dir)
            for path in written:
                with self.subTest(path=os.path.basename(path)):
                    self.assertEqual(Image.open(path).size, (32, 24))

    def test_returns_empty_list_when_camera_has_no_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            os.makedirs(snap_dir)
            with patch.object(S, "SNAPSHOTS", Path(snap_dir)):
                self.assertEqual(run_camera("overhead5", out_dir=os.path.join(tmp, "o")), [])


class MainTest(unittest.TestCase):
    def test_runs_all_three_cameras_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            os.makedirs(snap_dir)
            seen = []
            with patch.object(S, "SNAPSHOTS", Path(snap_dir)), \
                 patch("python.labeling.merge_overhead.run_camera",
                       side_effect=lambda cam, **kw: seen.append(cam) or []):
                main([])
            self.assertEqual(seen, list(CAMERAS))

    def test_honours_explicit_camera_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            os.makedirs(snap_dir)
            seen = []
            with patch.object(S, "SNAPSHOTS", Path(snap_dir)), \
                 patch("python.labeling.merge_overhead.run_camera",
                       side_effect=lambda cam, **kw: seen.append(cam) or []):
                main(["--cameras", "overhead6"])
            self.assertEqual(seen, ["overhead6"])

    def test_forwards_tuning_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            os.makedirs(snap_dir)
            captured = {}
            with patch.object(S, "SNAPSHOTS", Path(snap_dir)), \
                 patch("python.labeling.merge_overhead.run_camera",
                       side_effect=lambda cam, **kw: captured.update(kw) or []):
                main(["--cameras", "overhead5", "--thresh", "55",
                      "--band-rows", "64", "--out-dir", tmp])
            self.assertEqual(captured["thresh"], 55.0)
            self.assertEqual(captured["band_rows"], 64)
            self.assertEqual(captured["out_dir"], tmp)

    def test_rejects_scale_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            os.makedirs(snap_dir)
            with patch.object(S, "SNAPSHOTS", Path(snap_dir)):
                with self.assertRaises(SystemExit):
                    main(["--scale", "8"])

    def test_exits_when_snapshot_dir_missing(self):
        with patch.object(S, "SNAPSHOTS", Path("/definitely/not/here")):
            with self.assertRaises(SystemExit):
                main([])

    def test_surfaces_size_mismatch_as_clean_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            _write_snapshot(snap_dir, "raw_1783480173570_1", "overhead5",
                            _solid(10, 10, (20, 20, 20)))
            _write_snapshot(snap_dir, "raw_1783480173571_2", "overhead5",
                            _solid(12, 10, (20, 20, 20)))
            with patch.object(S, "SNAPSHOTS", Path(snap_dir)):
                with self.assertRaises(SystemExit) as ctx:
                    main(["--cameras", "overhead5",
                          "--out-dir", os.path.join(tmp, "out")])
            self.assertIn("帧尺寸不一致", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
