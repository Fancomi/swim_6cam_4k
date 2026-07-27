import os
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from python.annotation_preview import common as C
from python.annotation_preview.merge_overhead import (
    BAND_ROWS,
    CAMERAS,
    FrameSizeError,
    annotate,
    frame_color,
    load_stack,
    main,
    median_background,
    merge_frames,
    run_camera,
    snapshot_time_label,
    weighted_median,
)


def _solid(h, w, rgb):
    """构造一张纯色帧。"""
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :] = rgb
    return frame


class WeightedMedianTest(unittest.TestCase):
    def test_returns_none_for_empty_histogram(self):
        self.assertIsNone(weighted_median(np.zeros(8, dtype=np.int64)))

    def test_picks_middle_of_three(self):
        hist = np.zeros(10, dtype=np.int64)
        hist[[1, 2, 9]] = 1
        self.assertEqual(weighted_median(hist), 2)

    def test_takes_lower_middle_on_even_count(self):
        hist = np.zeros(10, dtype=np.int64)
        hist[[3, 7]] = 1
        self.assertEqual(weighted_median(hist), 3)

    def test_respects_weights(self):
        hist = np.zeros(5, dtype=np.int64)
        hist[0] = 10
        hist[4] = 1
        self.assertEqual(weighted_median(hist), 0)


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
        merged, _anchors = merge_frames(np.stack([early, late]), bg, thresh=40)
        self.assertTrue((merged[:, :, 0] == 200).all())

    def test_below_threshold_pixels_keep_background(self):
        bg = _solid(2, 2, (100, 100, 100))
        quiet = _solid(2, 2, (110, 100, 100))          # 距离 10 < 40
        merged, anchors = merge_frames(np.stack([quiet]), bg, thresh=40)
        np.testing.assert_array_equal(merged, bg)
        self.assertEqual(anchors, [None])

    def test_anchor_is_component_wise_median_of_foreground(self):
        bg = np.zeros((9, 9, 3), dtype=np.uint8)
        frame = np.zeros((9, 9, 3), dtype=np.uint8)
        for y, x in ((1, 1), (4, 6), (7, 2)):
            frame[y, x] = (255, 255, 255)
        _merged, anchors = merge_frames(np.stack([frame]), bg, thresh=40)
        self.assertEqual(anchors, [(2, 4)])            # x 中位 2，y 中位 4

    def test_band_split_matches_single_pass(self):
        rng = np.random.default_rng(11)
        stack = rng.integers(0, 256, size=(4, 13, 11, 3), dtype=np.uint8)
        bg = median_background(stack, band_rows=10_000)
        whole, whole_anchors = merge_frames(stack, bg, band_rows=10_000)
        for band_rows in (1, 5, 13, 10_000):
            with self.subTest(band_rows=band_rows):
                merged, anchors = merge_frames(stack, bg, band_rows=band_rows)
                np.testing.assert_array_equal(merged, whole)
                self.assertEqual(anchors, whole_anchors)

    def test_does_not_mutate_inputs(self):
        bg = _solid(3, 3, (0, 0, 0))
        stack = np.stack([_solid(3, 3, (200, 0, 0))])
        before_bg, before_stack = bg.copy(), stack.copy()
        merge_frames(stack, bg, thresh=40)
        np.testing.assert_array_equal(bg, before_bg)
        np.testing.assert_array_equal(stack, before_stack)


class SnapshotTimeLabelTest(unittest.TestCase):
    def test_parses_millisecond_timestamp_in_local_time(self):
        import datetime

        expected = datetime.datetime.fromtimestamp(1783480173.576).strftime("%H:%M:%S")
        self.assertEqual(snapshot_time_label("raw_1783480173576_15"), expected)

    def test_falls_back_to_id_when_unparseable(self):
        self.assertEqual(snapshot_time_label("weird_name"), "weird_name")


class FrameColorTest(unittest.TestCase):
    def test_is_deterministic(self):
        self.assertEqual(frame_color(3, 10), frame_color(3, 10))

    def test_distinct_indices_differ(self):
        self.assertNotEqual(frame_color(0, 10), frame_color(5, 10))

    def test_returns_three_bytes(self):
        color = frame_color(2, 7)
        self.assertEqual(len(color), 3)
        for channel in color:
            self.assertIsInstance(channel, int)
            self.assertGreaterEqual(channel, 0)
            self.assertLessEqual(channel, 255)

    def test_first_and_last_frame_colours_are_clearly_distinguishable(self):
        first = frame_color(0, 50)
        last = frame_color(49, 50)
        distance2 = sum((a - b) ** 2 for a, b in zip(first, last))
        self.assertGreater(distance2, 100 ** 2)


class AnnotateTest(unittest.TestCase):
    def test_appends_legend_band_below_image(self):
        merged = _solid(40, 60, (0, 0, 0))
        labels = [(1, "11:09:33"), (2, "11:11:45")]
        out = annotate(merged, [(10, 10), (20, 20)], labels)
        self.assertEqual(out.shape[1], 60)
        self.assertGreater(out.shape[0], 40)
        self.assertEqual(out.dtype, np.uint8)

    def test_draws_something_near_anchor(self):
        merged = _solid(40, 60, (0, 0, 0))
        out = annotate(merged, [(30, 20)], [(1, "11:09:33")])
        self.assertTrue((out[10:31, 20:41] > 0).any())

    def test_missing_anchor_leaves_image_region_untouched(self):
        merged = _solid(40, 60, (7, 7, 7))
        out = annotate(merged, [None], [(1, "11:09:33")])
        np.testing.assert_array_equal(out[:40], merged)

    def test_does_not_mutate_input(self):
        merged = _solid(40, 60, (0, 0, 0))
        before = merged.copy()
        annotate(merged, [(30, 20)], [(1, "11:09:33")])
        np.testing.assert_array_equal(merged, before)


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

    def test_downscales_by_integer_factor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.png")
            Image.fromarray(_solid(8, 12, (30, 30, 30))).save(path)
            stack = load_stack([path], scale=2)
            self.assertEqual(stack.shape, (1, 4, 6, 3))

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
    def test_writes_three_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            out_dir = os.path.join(tmp, "out")
            quiet = _solid(24, 32, (60, 60, 60))
            loud = quiet.copy()
            loud[4:8, 4:8] = (250, 10, 10)
            for i, frame in enumerate((quiet, quiet, loud)):
                _write_snapshot(snap_dir, "raw_178348017357%d_%d" % (i, i + 1),
                                "overhead5", frame)
            with patch.object(C, "SNAP_DIR", snap_dir):
                written = run_camera("overhead5", out_dir=out_dir)
            names = sorted(os.path.basename(p) for p in written)
            self.assertEqual(names, [
                "overhead5_background.png",
                "overhead5_merged.png",
                "overhead5_merged_labeled.png",
            ])
            for path in written:
                self.assertTrue(os.path.exists(path), path)

    def test_merged_keeps_source_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            out_dir = os.path.join(tmp, "out")
            for i in range(3):
                _write_snapshot(snap_dir, "raw_178348017357%d_%d" % (i, i + 1),
                                "overhead5", _solid(24, 32, (60, 60, 60)))
            with patch.object(C, "SNAP_DIR", snap_dir):
                run_camera("overhead5", out_dir=out_dir)
            merged = Image.open(os.path.join(out_dir, "overhead5_merged.png"))
            self.assertEqual(merged.size, (32, 24))

    def test_returns_empty_list_when_camera_has_no_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            os.makedirs(snap_dir)
            with patch.object(C, "SNAP_DIR", snap_dir):
                self.assertEqual(run_camera("overhead5", out_dir=os.path.join(tmp, "o")), [])


class MainTest(unittest.TestCase):
    def test_runs_all_three_cameras_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            os.makedirs(snap_dir)
            seen = []
            with patch.object(C, "SNAP_DIR", snap_dir), \
                 patch("python.annotation_preview.merge_overhead.run_camera",
                       side_effect=lambda cam, **kw: seen.append(cam) or []):
                main([])
            self.assertEqual(seen, list(CAMERAS))

    def test_honours_explicit_camera_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            os.makedirs(snap_dir)
            seen = []
            with patch.object(C, "SNAP_DIR", snap_dir), \
                 patch("python.annotation_preview.merge_overhead.run_camera",
                       side_effect=lambda cam, **kw: seen.append(cam) or []):
                main(["--cameras", "overhead6"])
            self.assertEqual(seen, ["overhead6"])

    def test_forwards_tuning_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap_dir = os.path.join(tmp, "snapshots")
            os.makedirs(snap_dir)
            captured = {}
            with patch.object(C, "SNAP_DIR", snap_dir), \
                 patch("python.annotation_preview.merge_overhead.run_camera",
                       side_effect=lambda cam, **kw: captured.update(kw) or []):
                main(["--cameras", "overhead5", "--thresh", "55",
                      "--band-rows", "64", "--scale", "4", "--out-dir", tmp])
            self.assertEqual(captured["thresh"], 55.0)
            self.assertEqual(captured["band_rows"], 64)
            self.assertEqual(captured["scale"], 4)
            self.assertEqual(captured["out_dir"], tmp)

    def test_exits_when_snapshot_dir_missing(self):
        with patch.object(C, "SNAP_DIR", "/definitely/not/here"):
            with self.assertRaises(SystemExit):
                main([])


if __name__ == "__main__":
    unittest.main()
