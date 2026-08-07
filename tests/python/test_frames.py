"""frames 统一入口的单元测试：organize / auto_merge / merge / grid / screen。

覆盖合并前五个模块（organize_under / object_frames / snapshot_frames /
mask_merge / mask_grid）的全部核心行为，验证合并后功能不丢。
跑法：.venv/bin/python -m unittest tests.python.test_frames
"""
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
            from python.common.media import read_image
            merged = read_image(paths[1])[:, :, ::-1]
            # mask 覆盖 (2,2) 取后帧 200；背景中值 (10+200)/2=105
            self.assertEqual(merged[2, 2].tolist(), [200, 200, 200])
            self.assertEqual(merged[0, 0].tolist(), [105, 105, 105])

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


if __name__ == "__main__":
    unittest.main()
