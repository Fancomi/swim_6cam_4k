import unittest

import numpy as np

from python.annotation_preview.merge_overhead import (
    BAND_ROWS,
    CAMERAS,
    annotate,
    frame_color,
    median_background,
    merge_frames,
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


if __name__ == "__main__":
    unittest.main()
