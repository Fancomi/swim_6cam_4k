"""frames 统一入口的单元测试：organize / auto_merge / merge / grid / screen。

覆盖合并前五个模块（organize_under / object_frames / snapshot_frames /
mask_merge / mask_grid）的全部核心行为，验证合并后功能不丢。
跑法：.venv/bin/python -m unittest tests.python.test_frames
"""
import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from python.labeling import frames as F


def make_snapshot(root, snap_id, camera, size=(8, 6), fill=(100, 100, 100),
                  ext=".jpg", date="20260807"):
    """在 root/<date>/snapshots/<snap_id>/ 下写一张 <camera> 的帧（jpg）。"""
    snap = Path(root) / str(date) / "snapshots" / snap_id
    snap.mkdir(parents=True, exist_ok=True)
    img = np.full((size[1], size[0], 3), fill, dtype=np.uint8)
    from PIL import Image
    Image.fromarray(img).save(snap / ("01_stitch__x__%s%s" % (camera, ext)))


class OrganizeTest(unittest.TestCase):
    def test_organize_camera_copies_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_snapshot(tmp, "raw_1_1", "underA1")
            make_snapshot(tmp, "raw_2_2", "underA1")
            with patch.object(F, "DATASET", Path(tmp)):
                out = F.object_frames_root("20260807")
                written = F.organize_camera("underA1", date="20260807",
                                            out_root=out)
                self.assertEqual(len(written), 2)
                files = sorted(Path(w).name for w in written)
                self.assertEqual(files[0], "f01_raw_1_1__01_stitch__x__underA1.jpg")

    def test_organize_missing_camera_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_snapshot(tmp, "raw_1_1", "underA1")
            with patch.object(F, "DATASET", Path(tmp)):
                out = F.object_frames_root("20260807")
                self.assertEqual(F.organize_camera("overhead5", date="20260807",
                                                   out_root=out), [])


class AutoMergeTest(unittest.TestCase):
    def test_auto_merge_produces_background_and_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_snapshot(tmp, "raw_1_1", "gemini_camera_1", fill=(10, 10, 10))
            make_snapshot(tmp, "raw_2_2", "gemini_camera_1", fill=(200, 200, 200))
            with patch.object(F, "DATASET", Path(tmp)):
                out = F.object_frames_root("20260807")
                written = F.auto_merge_camera("gemini_camera_1", date="20260807",
                                              out_root=out, band_rows=4)
                self.assertEqual(len(written), 2)
                names = sorted(Path(w).name for w in written)
                self.assertEqual(names,
                                 ["gemini_camera_1_background.png",
                                  "gemini_camera_1_merged.png"])
                # merged 里应有前景（第二帧的 200）覆盖背景（10/200 中值=105）
                from python.common.media import read_image
                merged = read_image(written[1])[:, :, ::-1]
                self.assertEqual(merged[0, 0].tolist(), [200, 200, 200])

    def test_auto_merge_requires_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(F, "DATASET", Path(tmp)):
                out = F.object_frames_root("20260807")
                self.assertEqual(F.auto_merge_camera("nope", date="20260807",
                                                     out_root=out), [])

    def test_auto_merge_across_dates(self):
        # 跨两个数据集（H 16 帧 + V 49 帧）收集同一相机帧当一段合成。
        with tempfile.TemporaryDirectory() as tmp:
            # 两个日期根：20260807-6cam-Horizontal / 20260807-6cam-Vertical
            for d, fills in (("20260807-6cam-Horizontal", (10, 10)),
                             ("20260807-6cam-Vertical", (100, 200))):
                for i, v in enumerate(fills, 1):
                    make_snapshot(tmp, "raw_%d_%d" % (i, i), "zcam_1",
                                  fill=(v, v, v), date=d)
            with patch.object(F, "DATASET", Path(tmp)):
                out = Path(tmp) / "obj"
                written = F.auto_merge_camera(
                    "zcam_1", dates=["20260807-6cam-Horizontal",
                                     "20260807-6cam-Vertical"],
                    out_root=out, band_rows=4)
                self.assertEqual(len(written), 2)
                from python.common.media import read_image
                merged = read_image(written[1])[:, :, ::-1]
                # 65 帧（实为 4 帧）中值背景 = 中值(10,10,100,200)=55；
                # 后帧 200 覆盖 -> merged 中心是 200。
                self.assertEqual(merged[0, 0].tolist(), [200, 200, 200])

    def test_merge_frames_noise_gate_filters_habitual_flicker(self):
        """noise_gate 用 MAD 滤掉"每帧都在晃"的像素，保留只在个别帧出现的目标。

        (5,5) 每帧都在 10/200 之间跳（常态波动大，MAD 高）→ 被门控滤掉；
        (0,0) 只有最后一帧跳到 200（MAD≈0）→ 被保留。用 PNG 避免 JPEG 有损。"""
        with tempfile.TemporaryDirectory() as tmp:
            for i in (1, 2, 3, 4):
                snap = Path(tmp) / "d" / "snapshots" / ("raw_%d_%d" % (i, i))
                snap.mkdir(parents=True)
                from PIL import Image
                img = np.full((20, 20, 3), 10, dtype=np.uint8)
                if i % 2 == 0:
                    img[5, 5] = 200          # 隔帧闪烁 → 常态波动（水花/灯光）
                if i == 4:
                    img[0, 0] = 200          # 只有一帧出现 → 瞬时目标（拉线）
                Image.fromarray(img).save(snap / "01_stitch__x__zcam_1.png")
            paths = [str(Path(tmp) / "d" / "snapshots" / ("raw_%d_%d" % (i, i)) /
                         "01_stitch__x__zcam_1.png") for i in (1, 2, 3, 4)]
            bg = F.median_background_streaming(paths, band_rows=4)
            # 不开门控：两处都算前景，(5,5) 也被叠上去
            plain = F.merge_frames_streaming(paths, bg, band_rows=4)
            self.assertEqual(plain[0, 0].tolist(), [200, 200, 200])
            self.assertEqual(plain[5, 5].tolist(), [200, 200, 200])
            # 开门控：(5,5) 常态波动大被滤掉，(0,0) 瞬时目标保留
            gated = F.merge_frames_streaming(paths, bg, band_rows=4, noise_gate=3.0)
            self.assertEqual(gated[0, 0].tolist(), [200, 200, 200])
            self.assertEqual(gated[5, 5].tolist(), bg[5, 5].tolist())

    def test_merge_frames_pick_peak_takes_max_deviation_frame(self):
        """pick='peak' 取偏离背景最大的那一帧，而非最后一帧。"""
        with tempfile.TemporaryDirectory() as tmp:
            # 帧2 偏离最大(250)，帧4 偏离较小(150)；last 取 150，peak 取 250
            for i, v in ((1, 10), (2, 250), (3, 10), (4, 150)):
                snap = Path(tmp) / "d" / "snapshots" / ("raw_%d_%d" % (i, i))
                snap.mkdir(parents=True)
                from PIL import Image
                img = np.full((20, 20, 3), 10, dtype=np.uint8)
                img[0, 0] = v
                Image.fromarray(img).save(snap / "01_stitch__x__zcam_1.png")
            paths = [str(Path(tmp) / "d" / "snapshots" / ("raw_%d_%d" % (i, i)) /
                         "01_stitch__x__zcam_1.png") for i in (1, 2, 3, 4)]
            bg = F.median_background_streaming(paths, band_rows=4)
            last = F.merge_frames_streaming(paths, bg, band_rows=4, pick="last")
            peak = F.merge_frames_streaming(paths, bg, band_rows=4, pick="peak")
            self.assertEqual(last[0, 0].tolist(), [150, 150, 150])
            self.assertEqual(peak[0, 0].tolist(), [250, 250, 250])


class MergeTest(unittest.TestCase):
    def _project(self, root, camera, strokes, n=2):
        snap_root = Path(root) / "20260807" / "snapshots"
        snaps = sorted(p.name for p in snap_root.iterdir() if p.is_dir())
        frames = []
        for i, s in enumerate(snaps[:n], 1):
            fname = [f for f in os.listdir(snap_root / s)
                     if f.endswith("__%s.jpg" % camera)][0]
            frames.append({"frame_index": i, "snapshot_id": s,
                           "image": "%s/%s" % (s, fname), "strokes": strokes})
        return {"schema": F.SCHEMA, "cameras": {camera: frames}}

    def test_merge_masks_foreground_over_median(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_snapshot(tmp, "raw_1_1", "gemini_camera_1", fill=(10, 10, 10))
            make_snapshot(tmp, "raw_2_2", "gemini_camera_1", fill=(200, 200, 200))
            proj = self._project(tmp, "gemini_camera_1",
                                 [{"x1": 2, "y1": 2, "x2": 2, "y2": 2, "r": 1}])
            out = Path(tmp) / "obj"
            paths = F.merge_camera("gemini_camera_1", proj["cameras"]["gemini_camera_1"],
                                   Path(tmp) / "20260807" / "snapshots", out)
            self.assertEqual(len(paths), 2)
            # 背景统一命名 <相机>_background.png（不再有 mask_background）
            self.assertEqual(Path(paths[0]).name, "gemini_camera_1_background.png")
            self.assertEqual(Path(paths[1]).name, "gemini_camera_1_mask_merged.png")
            from python.common.media import read_image
            merged = read_image(paths[1])[:, :, ::-1]
            # mask 覆盖 (2,2) 取后帧 200；背景中值 (10+200)/2=105
            self.assertEqual(merged[2, 2].tolist(), [200, 200, 200])
            self.assertEqual(merged[0, 0].tolist(), [105, 105, 105])

    def test_merge_background_uses_all_frames(self):
        """背景取 bg_paths（全部快照帧）中值，与 organize/auto_merge 口径一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            for i, v in enumerate((10, 100, 200), 1):
                make_snapshot(tmp, "raw_%d_%d" % (i, i), "gemini_camera_1",
                              fill=(v, v, v))
            snap = Path(tmp) / "20260807" / "snapshots"
            # 工程只含前 2 帧，背景仍应用全部 3 帧的中值（=100）
            proj = self._project(tmp, "gemini_camera_1", [], n=2)
            bg_paths = sorted(str(p) for p in snap.glob("raw_*/*.jpg"))
            out = Path(tmp) / "obj"
            paths = F.merge_camera("gemini_camera_1",
                                   proj["cameras"]["gemini_camera_1"], snap, out,
                                   bg_paths=bg_paths)
            from python.common.media import read_image
            bg = read_image(paths[0])[:, :, ::-1]
            self.assertEqual(bg[0, 0].tolist(), [100, 100, 100])

    def test_merge_annotates_frame_ids(self):
        """每块 mask 中心画 f<帧ID> 标签（改变了该处像素）。"""
        with tempfile.TemporaryDirectory() as tmp:
            for i in (1, 2):
                make_snapshot(tmp, "raw_%d_%d" % (i, i), "gemini_camera_1",
                              size=(200, 120), fill=(60, 60, 60))
            proj = self._project(tmp, "gemini_camera_1",
                                 [{"x1": 100, "y1": 60, "x2": 100, "y2": 60,
                                   "r": 10}])
            out = Path(tmp) / "obj"
            paths = F.merge_camera("gemini_camera_1",
                                   proj["cameras"]["gemini_camera_1"],
                                   Path(tmp) / "20260807" / "snapshots", out)
            from python.common.media import read_image
            merged = read_image(paths[1])
            # 标签是亮黄字 + 黑底，画在 mask 中心上方；应出现亮黄像素
            yellow = ((merged[:, :, 0] < 120) & (merged[:, :, 1] > 180)
                      & (merged[:, :, 2] > 200)).sum()
            self.assertGreater(yellow, 0)

    def test_merge_frame_index_by_snapshot_id(self):
        """同一相机不同快照的文件名相同，帧 ID 必须按 snapshot_id 匹配，不能按 basename。

        回归：basename 匹配会让所有帧都命中第一条（全标成 f01）。"""
        with tempfile.TemporaryDirectory() as tmp:
            # 两个快照目录，但文件用相同的名字（真实数据就是如此）
            for i, sid in enumerate(("raw_1_1", "raw_2_2"), 1):
                snap = Path(tmp) / "20260807" / "snapshots" / sid
                snap.mkdir(parents=True)
                from PIL import Image
                img = np.full((120, 200, 3), 60, dtype=np.uint8)
                Image.fromarray(img).save(
                    snap / "18_stitch__under-xlj-all__gemini_camera_1.jpg")
            proj = {"schema": F.SCHEMA, "cameras": {"gemini_camera_1": [
                {"frame_index": 1, "snapshot_id": "raw_1_1",
                 "image": "raw_1_1/18_stitch__under-xlj-all__gemini_camera_1.jpg",
                 "strokes": [{"x1": 100, "y1": 60, "x2": 100, "y2": 60, "r": 10}]},
                {"frame_index": 2, "snapshot_id": "raw_2_2",
                 "image": "raw_2_2/18_stitch__under-xlj-all__gemini_camera_1.jpg",
                 "strokes": [{"x1": 50, "y1": 60, "x2": 50, "y2": 60, "r": 10}]},
            ]}}
            out = Path(tmp) / "obj"
            paths = F.merge_camera("gemini_camera_1",
                                   proj["cameras"]["gemini_camera_1"],
                                   Path(tmp) / "20260807" / "snapshots", out)
            # 直接用 snapshot_id 查 frame_index（核心断言：不能全命中 f01）
            self.assertEqual(
                F._frame_index_of(proj["cameras"]["gemini_camera_1"], "raw_1_1"), 1)
            self.assertEqual(
                F._frame_index_of(proj["cameras"]["gemini_camera_1"], "raw_2_2"), 2)

    def test_resolve_images_across_multiple_roots(self):
        """跨数据集合成：工程帧分散在多个快照根下，逐个根反查。"""
        with tempfile.TemporaryDirectory() as tmp:
            for d in ("20260807-6cam-Horizontal", "20260807-6cam-Vertical"):
                make_snapshot(tmp, "raw_1_1", "gemini_camera_1", date=d)
            roots = [Path(tmp) / d / "snapshots"
                     for d in ("20260807-6cam-Horizontal", "20260807-6cam-Vertical")]
            proj_cam = [{"frame_index": 1, "snapshot_id": "raw_1_1",
                         "image": "raw_1_1/01_stitch__x__gemini_camera_1.jpg",
                         "strokes": []}]
            got = F._resolve_images(proj_cam, roots)
            self.assertEqual(len(got), 1)
            # 命中 Horizontal 根下的帧
            self.assertIn("20260807-6cam-Horizontal", got[0][1])

    def test_load_project_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "proj.json"
            p.write_text("not json", encoding="utf-8")
            with self.assertRaises(F.ProjectError):
                F.load_project(p)
            p.write_text(json.dumps({"schema": "other", "cameras": {}}),
                         encoding="utf-8")
            with self.assertRaises(F.ProjectError):
                F.load_project(p)


class GridTest(unittest.TestCase):
    def test_camera_meters_relative_to_lane_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "mesh.json"
            meshes = []
            for n, (x0, x1) in ((1, (2.0, 5.0)), (16, (-20.0, -17.0))):
                tri = [{"pos": [x0, 0.0, 0.0]}, {"pos": [x1, 0.0, 0.0]},
                       {"pos": [x0, 1.0, 0.0]}]
                meshes.append({"texture_basename": "underA%d-grid.png" % n,
                               "triangles": [tri]})
            p.write_text(json.dumps({"meshes": meshes}), encoding="utf-8")
            with patch.object(F, "UNDER_CAMERAS",
                              ("underA1", "underA16")):
                m = F.camera_meters(p)
                # lane_min = -20，A16 中心 -18.5 -> 1.5m，A1 中心 3.5 -> 23.5m
                self.assertEqual(m["underA16"], 1.5)
                self.assertEqual(m["underA1"], 23.5)

    def test_stitch_4x4_missing_tiles_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 只写 2 台图，其余缺
            for k in (1, 16):
                img = np.full((30, 40, 3), k * 15 % 256, dtype=np.uint8)
                from python.common.media import write_image
                write_image(Path(tmp) / ("underA%d_mask_merged.png" % k),
                            img, "t")
            meters = {c: float(i) for i, c in enumerate(F.UNDER_CAMERAS)}
            out = Path(tmp) / "grid.png"
            with patch.object(F, "UNDER_CAMERAS",
                              ("underA16", "underA15", "underA2", "underA1",
                               "underA14", "underA13", "underA12", "underA11",
                               "underA10", "underA9", "underA8", "underA7",
                               "underA6", "underA5", "underA4", "underA3")):
                path, info = F.stitch_4x4(tmp, out, meters)
                self.assertTrue(Path(path).is_file())
                states = [i["state"] for i in info]
                self.assertEqual(states.count("有图"), 2)
                self.assertEqual(states.count("缺图"), 14)


class ScreenTest(unittest.TestCase):
    def test_screen_writes_csvs(self):
        with tempfile.TemporaryDirectory() as tmp:
            for k in (1, 2, 3):
                make_snapshot(tmp, "raw_%d_%d" % (k, k), "underA1")
            with patch.object(F, "DATASET", Path(tmp)), \
                 patch.object(F, "UNDER_CAMERAS", ("underA1",)):
                out = F.object_frames_root("20260807")
                n_frames, n_curated = F.screen_underwater(
                    date="20260807", out_root=out, band_rows=4)
                self.assertEqual(n_frames, 3)
                det = Path(out) / "detections.csv"
                cur = Path(out) / "curated.csv"
                self.assertTrue(det.is_file())
                self.assertTrue(cur.is_file())
                rows = det.read_text().strip().splitlines()
                self.assertEqual(len(rows), 4)   # 表头 + 3
                self.assertEqual(rows[0].split(",")[0], "camera")


class CliTest(unittest.TestCase):
    def test_unknown_command_exits(self):
        with self.assertRaises(SystemExit):
            F._parser().parse_args(["nope"])

    def test_auto_merge_requires_camera(self):
        with self.assertRaises(SystemExit):
            F._parser().parse_args(["auto_merge", "--date", "20260807"])

    def test_all_subcommands_exist(self):
        for cmd in ("organize", "auto_merge", "merge", "grid", "label"):
            self.assertIn(cmd, F._parser()._subparsers._group_actions[0].choices)


class MeterSpecTest(unittest.TestCase):
    """米数口径来自数据侧 sidecar，不写死在代码里。"""

    def test_default_is_even_spacing(self):
        m = F.frame_meters(n_frames=5)
        self.assertEqual([m[i] for i in range(1, 6)], [0.5, 1.0, 1.5, 2.0, 2.5])

    def test_gaps_skip_one_position(self):
        # gaps=[2]：第2帧之后缺一帧，f3 比 f2 多两格（1.0 -> 2.0）
        m = F.frame_meters({"gaps": [2]}, n_frames=4)
        self.assertEqual([m[i] for i in range(1, 5)], [0.5, 1.0, 2.0, 2.5])

    def test_skip_marks_frame_unlabelled_without_consuming_position(self):
        # skip=[3]：第3帧是重复帧，不标且不占位，f4 接着 f2 继续
        m = F.frame_meters({"skip": [3]}, n_frames=4)
        self.assertEqual(m[1], 0.5)
        self.assertEqual(m[2], 1.0)
        self.assertIsNone(m[3])
        self.assertEqual(m[4], 1.5)

    def test_reproduces_20260807_recorded_anomalies(self):
        """回归：spec 驱动的表要与之前写死在代码里的 20260807 口径一致。"""
        spec = {"start": 0.5, "step": 0.5, "gaps": [28], "skip": [35],
                "n_frames": 51}
        m = F.frame_meters(spec)
        for f, want in ((1, 0.5), (28, 14.0), (29, 15.0), (33, 17.0),
                        (34, 17.5), (35, None), (36, 18.0), (51, 25.5)):
            self.assertEqual(m[f], want, "f%d" % f)

    def test_load_spec_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(F.load_meter_spec(Path(tmp) / "none.json"))

    def test_load_spec_rejects_wrong_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            p.write_text(json.dumps({"schema": "other", "gaps": []}),
                         encoding="utf-8")
            with self.assertRaises(F.ProjectError):
                F.load_meter_spec(p)

    def test_load_spec_reads_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "s.json"
            p.write_text(json.dumps({"schema": F.METER_SPEC_SCHEMA,
                                     "gaps": [28], "skip": [35]}),
                         encoding="utf-8")
            spec = F.load_meter_spec(p)
            self.assertEqual(spec["gaps"], [28])


class BandFitTest(unittest.TestCase):
    """MAD 要整条带装全部帧，帧数多时自动收窄带高，不靠调用方记得传参数。"""

    def test_shrinks_when_frames_would_blow_budget(self):
        # 300 帧 3840 宽：512MB 预算下装不下 216 行
        fit = F._fit_band_rows(216, 300, 3840, 2160)
        self.assertLess(fit, 216)
        self.assertGreaterEqual(fit, 1)
        self.assertLessEqual(300 * fit * 3840 * 3 * 4, F.MEM_BUDGET_BYTES)

    def test_keeps_value_for_small_inputs(self):
        self.assertEqual(F._fit_band_rows(64, 16, 640, 360), 64)

    def test_never_exceeds_image_height(self):
        self.assertLessEqual(F._fit_band_rows(9999, 2, 64, 48), 48)


class ProductsTest(unittest.TestCase):
    """四类产物的配方是数据，products 子命令照它执行（不再靠人照文档敲命令）。"""

    def test_recipe_covers_four_classes(self):
        self.assertEqual(sorted(F.PRODUCTS),
                         ["entry", "overhead", "sixcam", "underwater"])

    def test_sixcam_merges_both_dates_into_horizontal(self):
        steps = F.PRODUCTS["sixcam"]
        self.assertEqual(len(steps), 6)          # zcam1-4 + overhead5/6
        for s in steps:
            self.assertEqual(s["kind"], "auto_merge")
            self.assertEqual(list(s["dates"]), list(F.SIXCAM_DATES))
            self.assertEqual(s["out_date"], F.SIXCAM_DATES[0])
            self.assertGreater(s["noise_gate"], 0)   # 拉线场景必须开门控

    def test_overhead_fuses_two_datasets_into_20260708(self):
        step, = F.PRODUCTS["overhead"]
        self.assertEqual(step["out_date"], "20260708")
        self.assertEqual(len(step["dates"]), 2)

    def test_entry_runs_each_dataset_separately(self):
        steps = F.PRODUCTS["entry"]
        # 每步只有一个数据集（单数据集出，不融合）
        for s in steps:
            self.assertEqual(len(s["dates"]), 1)
        # 20260708 用旧相机名
        self.assertIn(("orbbec_camera_1",),
                      [tuple(s["cameras"]) for s in steps])

    def test_dry_run_touches_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(F, "DATASET", Path(tmp)):
                F.cmd_products(argparse.Namespace(
                    only="sixcam", dry_run=True, date="20260807"))
            # dry-run 不该建任何目录
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_unknown_product_exits(self):
        with self.assertRaises(SystemExit):
            F.cmd_products(argparse.Namespace(
                only="nope", dry_run=True, date="20260807"))


class AnnotateTest(unittest.TestCase):
    """标签绘制：位置、避让、跳过未标米数的帧。"""

    def _blank(self, h=200, w=400):
        return np.full((h, w, 3), 60, dtype=np.uint8)

    def _stroke(self, cx, cy, r=10):
        return {"x1": cx, "y1": cy, "x2": cx, "y2": cy, "r": r}

    def test_label_sits_above_mask_top(self):
        img = self._blank()
        F._annotate_masks(img, [(1, [self._stroke(200, 120)])], {1: 0.5})
        # mask 顶边 = 120-10 = 110；标签应画在其上方，不压 mask 中心
        painted = np.where((img != 60).any(axis=2))
        self.assertTrue(painted[0].size > 0)
        self.assertLess(painted[0].max(), 120)

    def test_skips_frames_with_no_meter(self):
        img = self._blank()
        F._annotate_masks(img, [(35, [self._stroke(200, 120)])], {35: None})
        self.assertTrue((img == 60).all())        # 一个像素都没画

    def test_frame_id_only_when_meters_disabled(self):
        a, b = self._blank(), self._blank()
        F._annotate_masks(a, [(7, [self._stroke(200, 120)])], {7: 3.5},
                          with_meters=True)
        F._annotate_masks(b, [(7, [self._stroke(200, 120)])], {7: 3.5},
                          with_meters=False)
        # 带米数的标签更宽，涂到的像素更多
        self.assertGreater((a != 60).sum(), (b != 60).sum())

    def test_overlapping_labels_do_not_stack_on_one_row(self):
        img = self._blank()
        # 三个 mask 中心同高、横向紧邻，标签必须错开行
        strokes = [(i + 1, [self._stroke(100 + i * 12, 120)]) for i in range(3)]
        F._annotate_masks(img, strokes, {1: 0.5, 2: 1.0, 3: 1.5})
        rows = sorted(set(np.where((img != 60).any(axis=2))[0].tolist()))
        # 若三条挤在同一行，涂到的行数会接近单条标签高度（<20）
        self.assertGreater(len(rows), 20)

    def test_place_label_gives_up_when_image_too_small(self):
        tiny = np.zeros((6, 6, 3), dtype=np.uint8)
        self.assertIsNone(F._place_label(tiny, "f01 0.5m", 3, 3, []))


class DateOfSnapshotTest(unittest.TestCase):
    """跨数据集帧要按 --dates 顺序分组，靠快照目录归属判断属于哪批。"""

    def test_finds_owning_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            for d in ("A", "B"):
                (Path(tmp) / d / "snapshots" / ("raw_%s_1" % d)).mkdir(parents=True)
            with patch.object(F, "DATASET", Path(tmp)):
                self.assertEqual(F._date_of_snapshot("raw_A_1", ["A", "B"]), "A")
                self.assertEqual(F._date_of_snapshot("raw_B_1", ["A", "B"]), "B")

    def test_falls_back_to_first_date_when_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(F, "DATASET", Path(tmp)):
                self.assertEqual(F._date_of_snapshot("raw_X_9", ["A", "B"]), "A")


class MergedSuffixTest(unittest.TestCase):
    """产物名进代码：水下留 mask_merged 给 grid 读，其余直接出交付名 merged。"""

    def test_suffix_is_configurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "20260807" / "snapshots" / "raw_1_1"
            snap.mkdir(parents=True)
            from PIL import Image
            Image.fromarray(np.full((8, 8, 3), 40, dtype=np.uint8)).save(
                snap / "01_stitch__x__gemini_camera_1.jpg")
            cam = [{"frame_index": 1, "snapshot_id": "raw_1_1",
                    "image": "raw_1_1/01_stitch__x__gemini_camera_1.jpg",
                    "strokes": []}]
            out = Path(tmp) / "obj"
            paths = F.merge_camera("gemini_camera_1", cam,
                                   Path(tmp) / "20260807" / "snapshots", out,
                                   merged_suffix="merged")
            self.assertEqual(Path(paths[1]).name, "gemini_camera_1_merged.png")


class RenumberTest(unittest.TestCase):
    """帧号重编号只在真跨数据集时做——单数据集重编号会把全局帧号压成 1..N。"""

    def _dataset(self, tmp, date, snaps, cam="underA11", start_index=27):
        """建一个数据集：snaps 个快照 + 工程（frame_index 从 start_index 起）。"""
        frames = []
        for i, sid in enumerate(snaps):
            snap = Path(tmp) / date / "snapshots" / sid
            snap.mkdir(parents=True, exist_ok=True)
            from PIL import Image
            Image.fromarray(np.full((8, 8, 3), 40 + i, dtype=np.uint8)).save(
                snap / ("01_stitch__x__%s.jpg" % cam))
            frames.append({"frame_index": start_index + i, "snapshot_id": sid,
                           "image": "%s/01_stitch__x__%s.jpg" % (sid, cam),
                           "strokes": [{"x1": 4, "y1": 4, "x2": 4, "y2": 4,
                                        "r": 1}]})
        proj = Path(tmp) / date / "snapshots" / F.PROJECT_FILENAME
        proj.write_text(json.dumps({"schema": F.SCHEMA, "cameras": {cam: frames}}),
                        encoding="utf-8")
        return frames

    def _run(self, tmp, dates, cam="underA11"):
        seen = {}

        def fake_merge(camera, project_cam, roots, out, **kw):
            seen[camera] = [f["frame_index"] for f in project_cam]
            return ["bg", "mg"]

        with patch.object(F, "DATASET", Path(tmp)), \
             patch.object(F, "merge_camera", fake_merge):
            F.cmd_merge(argparse.Namespace(
                project=None, date=dates[0],
                dates=list(dates) if len(dates) > 1 else None,
                root=str(tmp), snapshots=None, cameras=[cam], out_root=str(tmp),
                band_rows=64, meter_spec=None, meter_overrides=None,
                merged_suffix=None))
        return seen[cam]

    def test_single_dataset_keeps_global_frame_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._dataset(tmp, "20260807", ["raw_a_1", "raw_a_2", "raw_a_3"],
                          start_index=27)
            # 工程里是 f27~f29（该相机在全局时间轴上的位置），不能被压成 f1~f3
            self.assertEqual(self._run(tmp, ["20260807"]), [27, 28, 29])

    def test_cross_dataset_renumbers_from_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._dataset(tmp, "A", ["raw_a_1", "raw_a_2"], start_index=27)
            self._dataset(tmp, "B", ["raw_b_1"], start_index=5)
            # 两批各自从自己的号起，合并后必须统一重编号
            self.assertEqual(self._run(tmp, ["A", "B"]), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
