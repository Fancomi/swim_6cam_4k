import json
import os
import tempfile
import unittest

from python.water_entry import common as C
from python.water_entry import export_package as E
from python.water_entry import select_frames as S


def _rec(frame, sho_y=None, hip_y=None, conf=0.9):
    """构造一条 per_frame 记录：只设肩/胯 y，其他关键点置零置低分。"""
    if sho_y is None:
        return {"frame": frame, "kps_xy": None, "kps_conf": None}
    xy = [[0.0, 0.0]] * 17
    cf = [0.0] * 17
    for i in (C.L_SHO, C.R_SHO):
        xy[i] = [100.0, sho_y]
        cf[i] = conf
    for i in (C.L_HIP, C.R_HIP):
        xy[i] = [100.0, hip_y]
        cf[i] = conf
    return {"frame": frame, "kps_xy": xy, "kps_conf": cf}


class EstimateEntryFrameTest(unittest.TestCase):
    def test_returns_frame_where_shoulder_drops_below_hip(self):
        per_frame = [_rec(10, 100, 200), _rec(11, 150, 200),
                     _rec(12, 220, 200), _rec(13, 260, 200)]
        entry, signs = C.estimate_entry_frame(per_frame)
        self.assertEqual(entry, 12)
        self.assertEqual([f for f, _d in signs], [10, 11, 12, 13])

    def test_ignores_flip_before_jump_frame(self):
        # 起跳前扶壁蜷缩造成一次伪翻转（f10->f11），真入水在 f21
        per_frame = [_rec(10, 100, 200), _rec(11, 220, 200),
                     _rec(20, 100, 200), _rec(21, 230, 200)]
        self.assertEqual(C.estimate_entry_frame(per_frame)[0], 11)
        self.assertEqual(C.estimate_entry_frame(per_frame, after_frame=20)[0], 21)

    def test_skips_missing_and_low_confidence_frames(self):
        per_frame = [_rec(10, 100, 200), _rec(11), _rec(12, 100, 200, conf=0.1),
                     _rec(13, 240, 200)]
        entry, signs = C.estimate_entry_frame(per_frame)
        self.assertEqual(entry, 13)
        self.assertIsNone(signs[1][1])
        self.assertIsNone(signs[2][1])

    def test_returns_none_when_no_flip(self):
        per_frame = [_rec(10, 100, 200), _rec(11, 120, 200)]
        self.assertIsNone(C.estimate_entry_frame(per_frame)[0])


class TorsoOkTest(unittest.TestCase):
    def test_requires_all_four_torso_keypoints(self):
        conf = [0.0] * 17
        for i in C.TORSO_KPS:
            conf[i] = 0.9
        self.assertTrue(C.torso_ok(conf))
        conf[C.R_HIP] = C.KP_CONF - 0.01
        self.assertFalse(C.torso_ok(conf))


def _det(frame, *centres):
    return {"frame": frame,
            "boxes": [[cx - 10, 100, cx + 10, 200] for cx in centres]}


class LinkTracksTest(unittest.TestCase):
    def test_links_same_person_across_frames(self):
        dets = [_det(0, 100, 900), _det(1, 110, 905), _det(2, 120, 910)]
        tracks = C.link_tracks(dets, frame_width=1280)
        self.assertEqual(sorted(len(t) for t in tracks), [3, 3])

    def test_bridges_gap_within_max_gap(self):
        dets = [_det(0, 100), _det(1), _det(2), _det(3, 130)]
        tracks = C.link_tracks(dets, frame_width=1280, max_gap=6)
        self.assertEqual([len(t) for t in tracks], [2])

    def test_splits_when_gap_exceeds_max_gap(self):
        dets = [_det(0, 100), _det(1), _det(2), _det(3, 130)]
        tracks = C.link_tracks(dets, frame_width=1280, max_gap=1)
        self.assertEqual(sorted(len(t) for t in tracks), [1, 1])

    def test_match_radius_does_not_widen_with_gap_length(self):
        """跨缺口时半径必须保持固定，否则会接上画面里的静止无关目标。

        实测放大半径会让 swimup 的 12 条片段空中段检出下降（10 条归零）：
        20260713-103240 上一条跟住运动员 46 帧的轨迹被换成 cx≈1028 的 3 帧静态目标。
        """
        far = 100 + 1280 * 0.15 + 20      # 略超出固定半径
        dets = [_det(0, 100), _det(1), _det(2), _det(3), _det(4), _det(5, far)]
        tracks = C.link_tracks(dets, frame_width=1280, max_gap=6)
        self.assertEqual(sorted(len(t) for t in tracks), [1, 1])


class PickAthleteTrackTest(unittest.TestCase):
    def test_prefers_largest_displacement_along_swim_direction(self):
        # 本场地方向为右→左：运动员 cx 900->760，池边站立者几乎不动
        dets = [_det(0, 900, 1230), _det(1, 830, 1230), _det(2, 760, 1229)]
        tracks = C.link_tracks(dets, frame_width=1280)
        picked = C.pick_athlete_track(tracks, dets, left_to_right=False)
        first_cx = C._centre(C.detections_box(dets, *picked[0]))[0]
        self.assertEqual(first_cx, 900.0)

    def test_respects_left_to_right_direction(self):
        dets = [_det(0, 300, 1230), _det(1, 370, 1230), _det(2, 440, 1229)]
        tracks = C.link_tracks(dets, frame_width=1280)
        picked = C.pick_athlete_track(tracks, dets, left_to_right=True)
        first_cx = C._centre(C.detections_box(dets, *picked[0]))[0]
        self.assertEqual(first_cx, 300.0)

    def test_returns_none_when_all_tracks_too_short(self):
        dets = [_det(0, 100)]
        tracks = C.link_tracks(dets, frame_width=1280)
        self.assertIsNone(C.pick_athlete_track(tracks, dets, False, min_len=3))


class ClipWindowTest(unittest.TestCase):
    def _clip(self, **kw):
        base = dict(name="c", jump_frame=85, water_frame=88, angle=0.0,
                    backstroke_applied=False, note="")
        base.update(kw)
        return C.Clip(**base)

    def test_ref_entry_prefers_backstroke_entry_frame(self):
        clip = self._clip(bk_entry_frame=119)
        self.assertEqual(clip.ref_entry_frame, 119)
        self.assertEqual(clip.entry_source, "backstroke")

    def test_ref_entry_falls_back_to_manifest_water_frame(self):
        clip = self._clip(bk_entry_frame=-1)
        self.assertEqual(clip.ref_entry_frame, 88)
        self.assertEqual(clip.entry_source, "manifest_water_frame")

    def test_window_covers_both_entry_conventions(self):
        clip = self._clip(bk_entry_frame=119, bk_jump_frame=85)
        window = clip.window(pre=5, post=20)
        self.assertEqual(window[0], 80)
        self.assertEqual(window[-1], 139)

    def test_window_clamps_lower_bound_to_zero(self):
        clip = self._clip(jump_frame=2, bk_jump_frame=2, water_frame=6,
                          bk_entry_frame=6)
        self.assertEqual(clip.window(pre=5, post=1)[0], 0)


THRESH = {"iou": 0.4, "kp_mean_norm": 0.10}


def _pose(sho_y, hip_y, offset=0.0, torso_conf=0.9, box=(100, 100, 200, 300)):
    """构造一条 predict per_frame 记录：躯干四点 + 一个偏移量制造关键点分歧。"""
    xy = [[150.0 + offset, 200.0 + offset]] * 17
    cf = [0.0] * 17
    for i in (C.L_SHO, C.R_SHO):
        xy[i] = [150.0 + offset, sho_y]
        cf[i] = torso_conf
    for i in (C.L_HIP, C.R_HIP):
        xy[i] = [150.0 + offset, hip_y]
        cf[i] = torso_conf
    return {"frame": 100, "n_det": 1, "box": list(box), "conf": 0.9,
            "kps_xy": xy, "kps_conf": cf}


def _null(n_det=0):
    return {"frame": 100, "n_det": n_det, "box": None, "conf": None,
            "kps_xy": None, "kps_conf": None}


def _reasons(rec_a, rec_b):
    return S.analyze_frame(rec_a, rec_b, 100, 90, 100, THRESH)[0]


class AnalyzeFrameTest(unittest.TestCase):
    def test_both_blind_when_neither_model_detected_anything(self):
        self.assertEqual(_reasons(_null(0), _null(0)), ["both_blind"])

    def test_both_reject_when_detections_existed_but_none_selected(self):
        self.assertEqual(_reasons(_null(2), _null(1)), ["both_reject"])

    def test_one_miss_when_only_one_model_has_a_box(self):
        self.assertEqual(_reasons(_pose(100, 200), _null(1)), ["one_miss"])
        self.assertEqual(_reasons(_null(0), _pose(100, 200)), ["one_miss"])

    def test_diff_person_when_boxes_barely_overlap(self):
        far = _pose(100, 200, box=(600, 100, 700, 300))
        self.assertIn("diff_person", _reasons(_pose(100, 200), far))

    def test_diff_person_suppresses_keypoint_comparison(self):
        # 两框指向不同的人时，比较关键点没有意义，不应再叠加 kp_disagree
        far = _pose(300, 100, offset=80.0, box=(600, 100, 700, 300))
        self.assertNotIn("kp_disagree", _reasons(_pose(100, 200), far))

    def test_kp_disagree_only_above_threshold(self):
        same = _pose(100, 200)
        self.assertEqual(_reasons(same, _pose(100, 200, offset=2.0)), [])
        self.assertIn("kp_disagree", _reasons(same, _pose(100, 200, offset=60.0)))

    def test_sign_flip_when_models_disagree_on_shoulder_hip_order(self):
        # 肩在胯上 vs 肩在胯下 —— 入水判据的直接冲突
        self.assertIn("sign_flip", _reasons(_pose(100, 200), _pose(200, 100)))

    def test_torso_broken_when_either_model_lacks_torso_points(self):
        weak = _pose(100, 200, torso_conf=0.2)
        self.assertIn("torso_broken", _reasons(_pose(100, 200), weak))

    def test_clean_agreement_yields_no_reasons(self):
        self.assertEqual(_reasons(_pose(100, 200), _pose(101, 201)), [])


class PhaseAndScoreTest(unittest.TestCase):
    def test_phase_classification(self):
        self.assertEqual(S.phase_of(100, 90, 100), "entry")
        self.assertEqual(S.phase_of(95, 90, 100), "flight")
        self.assertEqual(S.phase_of(85, 90, 100), "pre")
        self.assertEqual(S.phase_of(115, 90, 100), "post")

    def test_entry_phase_wins_over_flight(self):
        # 入水前 2 帧同时属于飞行段，但应归为 entry（业务上更关键）
        self.assertEqual(S.phase_of(98, 90, 100), "entry")

    def test_score_takes_max_signal_not_sum(self):
        strong = S.score_frame(["both_blind"], "post", {})
        many_weak = S.score_frame(["kp_disagree", "torso_broken"], "post", {})
        self.assertGreater(strong, many_weak)

    def test_entry_phase_outranks_same_signal_elsewhere(self):
        self.assertGreater(S.score_frame(["sign_flip"], "entry", {}),
                           S.score_frame(["sign_flip"], "post", {}))

    def test_pre_phase_is_discounted(self):
        self.assertLess(S.score_frame(["one_miss"], "pre", {}),
                        S.score_frame(["one_miss"], "flight", {}))


def _row(clip, frame, score):
    return {"clip": clip, "frame": frame, "score": score}


class DedupeTest(unittest.TestCase):
    def test_keeps_highest_scoring_frame_within_gap(self):
        rows = [_row("a", 100, 10.0), _row("a", 101, 50.0), _row("a", 102, 20.0)]
        kept = S.dedupe(rows, min_gap=3)
        self.assertEqual([r["frame"] for r in kept], [101])

    def test_keeps_frames_spaced_at_least_min_gap(self):
        rows = [_row("a", 100, 10.0), _row("a", 103, 10.0), _row("a", 106, 10.0)]
        self.assertEqual(len(S.dedupe(rows, min_gap=3)), 3)

    def test_does_not_suppress_across_clips(self):
        rows = [_row("a", 100, 10.0), _row("b", 100, 10.0)]
        self.assertEqual(len(S.dedupe(rows, min_gap=5)), 2)

    def test_min_gap_one_is_a_passthrough(self):
        rows = [_row("a", 100, 1.0), _row("a", 101, 1.0)]
        self.assertEqual(len(S.dedupe(rows, min_gap=1)), 2)


class CapPerClipTest(unittest.TestCase):
    def test_keeps_top_scoring_frames_per_clip(self):
        rows = [_row("a", 1, 5.0), _row("a", 2, 9.0), _row("a", 3, 1.0),
                _row("b", 1, 2.0)]
        kept = S.cap_per_clip(rows, 2)
        self.assertEqual(sorted(r["frame"] for r in kept if r["clip"] == "a"), [1, 2])
        self.assertEqual(len([r for r in kept if r["clip"] == "b"]), 1)

    def test_zero_limit_is_a_passthrough(self):
        rows = [_row("a", i, 1.0) for i in range(5)]
        self.assertEqual(len(S.cap_per_clip(rows, 0)), 5)


class CollectFilterTest(unittest.TestCase):
    """collect() 的片段级过滤：入水帧不可信的片段必须被排除。"""

    def _write_pair(self, root, clip, entry_source, frames=(100,)):
        for model in (S.MODEL_A, S.MODEL_B):
            d = os.path.join(root, model, "per_frame")
            os.makedirs(d, exist_ok=True)
            payload = {"clip": clip, "jump_frame": 90, "entry_frame": 100,
                       "entry_source": entry_source,
                       "manifest_water_frame": 100, "left_to_right": False,
                       "frames": [{"frame": f, "n_det": 0, "box": None,
                                   "conf": None, "kps_xy": None,
                                   "kps_conf": None} for f in frames]}
            with open(os.path.join(d, clip + ".json"), "w") as f:
                json.dump(payload, f)

    def test_skips_clips_whose_entry_frame_is_unverified(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_pair(root, "good", "backstroke")
            self._write_pair(root, "bad", "manifest_water_frame")
            rows, totals = S.collect(root, None, THRESH)
            self.assertEqual({r["clip"] for r in rows}, {"good"})
            self.assertEqual(totals["clips"], 1)

    def test_allows_unverified_entry_when_requested(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_pair(root, "bad", "manifest_water_frame")
            rows, _totals = S.collect(root, None, THRESH,
                                      require_verified_entry=False)
            self.assertEqual({r["clip"] for r in rows}, {"bad"})

    def test_max_offset_excludes_late_frames_from_the_denominator(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_pair(root, "good", "backstroke",
                             frames=(100, 104, 110))
            rows, totals = S.collect(root, None, THRESH, max_offset=6)
            self.assertEqual([r["frame"] for r in rows], [100, 104])
            self.assertEqual(totals["frames"], 3)
            self.assertEqual(totals["frames_in_range"], 2)


class ExportPackageTest(unittest.TestCase):
    """交付包：预标注来源与 COCO 可见位的约定。"""

    def test_prelabel_prefers_bk_then_falls_back(self):
        per_model = {S.MODEL_B: {5: _pose(100, 200)},
                     S.MODEL_A: {5: _pose(110, 210)}}
        self.assertEqual(E.pick_prelabel(per_model, 5)[0], S.MODEL_B)

        per_model[S.MODEL_B][5] = _null()
        self.assertEqual(E.pick_prelabel(per_model, 5)[0], S.MODEL_A)

        per_model[S.MODEL_A][5] = _null()
        self.assertEqual(E.pick_prelabel(per_model, 5), (None, None))

    def test_low_confidence_keypoints_are_marked_unlabelled(self):
        """置信度不足的点写成 v=0，标注工具会显示为「待补」而不是一个错点。"""
        rec = _pose(100, 200)          # 只有躯干四点达标，其余为 0 分
        ann = E.coco_annotation(1, 1, rec)
        self.assertEqual(ann["num_keypoints"], 4)
        vis = ann["keypoints"][2::3]
        self.assertEqual([i for i, v in enumerate(vis) if v == 2],
                         sorted(C.TORSO_KPS))
        for i, v in enumerate(vis):
            if v == 0:
                self.assertEqual(ann["keypoints"][i * 3:i * 3 + 2], [0, 0])

    def test_bbox_is_converted_to_coco_xywh(self):
        ann = E.coco_annotation(1, 1, _pose(100, 200, box=(10, 20, 110, 220)))
        self.assertEqual(ann["bbox"], [10.0, 20.0, 100.0, 200.0])
        self.assertEqual(ann["area"], 20000.0)

    def test_coco_skeleton_is_one_indexed(self):
        """COCO 的 skeleton 用 1-based 索引，我们内部的 SKELETON 是 0-based。"""
        pairs = E.COCO_CATEGORY["skeleton"]
        self.assertEqual(len(pairs), len(C.SKELETON))
        self.assertEqual(pairs[0], [C.SKELETON[0][0] + 1, C.SKELETON[0][1] + 1])
        self.assertTrue(all(1 <= a <= 17 and 1 <= b <= 17 for a, b in pairs))


if __name__ == "__main__":
    unittest.main()