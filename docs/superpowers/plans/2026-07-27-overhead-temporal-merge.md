# 高空相机全时段前景合成 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `overhead5` / `overhead6` / `orbbec_camera_1` 三台相机各自全时段的 50 张快照，各合成为一张 UV 参考图交给设计师。

**Architecture:** 新增单一模块 `python/annotation_preview/merge_overhead.py`。逐相机独立处理：50 帧解码进 uint8 栈 → 逐像素中值得背景帧 → 每帧与背景的 RGB 欧氏距离超阈判前景 → 按时间顺序叠加，后帧覆盖前帧。中值与差分按水平条带计算以压住 4K 尺寸的 float32 峰值内存。纯计算函数不碰文件系统，IO 只集中在 `run_camera`。

**Tech Stack:** Python 3.10、NumPy 2.2.6、Pillow 12.3.0、`unittest`（本仓库无 pytest）。

**Spec:** `docs/superpowers/specs/2026-07-27-overhead-temporal-merge-design.md`

## Global Constraints

- 命令一律在仓库根目录 `swim_fbx_demo/` 下执行；Python 解释器固定用 `.venv/bin/python`。
- 测试框架是 `unittest`，**不是 pytest**（venv 里没装）。运行方式：`.venv/bin/python -m unittest <模块路径> -v`。
- 基线：`.venv/bin/python -m unittest discover -s tests/python -t .` 为 `Ran 82 tests` / `OK`。每个 Task 结束时该命令必须仍然全绿，且总数只增不减。
- 前景判定阈值默认 `40`，取自 `python/annotation_preview/common.py:33` 的 `DIST_THRESH`，不新定义常量。
- 三台相机的 camera id 精确为 `overhead5`、`overhead6`、`orbbec_camera_1`。
- 分带默认高度 `BAND_ROWS = 256`。
- 输出目录默认 `outputs/annotation_preview/overhead-merge/`（`common.OUTPUT_ROOT` 之下）。
- **禁止修改** `python/annotation_preview/common.py`。
- 不得引入任何形状筛选、连通域分析、人物过滤或目标检测。前景全叠是明确的设计决定。
- 不做跨相机几何配准。
- 代码注释与打印信息用中文，与 `annotation_preview` 包内既有模块一致。

---

### Task 1: 分带中值背景与前景叠加

模块的计算内核。纯函数，不碰文件系统。

**Files:**
- Create: `python/annotation_preview/merge_overhead.py`
- Test: `tests/python/test_merge_overhead.py`

**Interfaces:**
- Consumes: `python.annotation_preview.common.DIST_THRESH`（值 `40`）。
- Produces:
  - `CAMERAS: tuple[str, ...]` = `("overhead5", "overhead6", "orbbec_camera_1")`
  - `BAND_ROWS: int` = `256`
  - `median_background(stack, band_rows=BAND_ROWS) -> np.ndarray`：入 `(N, H, W, 3) uint8`，出 `(H, W, 3) uint8`
  - `merge_frames(stack, background, thresh=C.DIST_THRESH, band_rows=BAND_ROWS) -> tuple[np.ndarray, list]`：出 `((H, W, 3) uint8, [ (x, y) | None ] * N)`
  - `weighted_median(hist) -> int | None`：直方图加权**下中位数**；全零直方图返回 `None`

- [ ] **Step 1: 写失败的测试**

创建 `tests/python/test_merge_overhead.py`：

```python
import unittest

import numpy as np

from python.annotation_preview.merge_overhead import (
    BAND_ROWS,
    CAMERAS,
    median_background,
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest tests.python.test_merge_overhead -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'python.annotation_preview.merge_overhead'`

- [ ] **Step 3: 写最小实现**

创建 `python/annotation_preview/merge_overhead.py`：

```python
#!/usr/bin/env python3
"""把一台相机全时段的快照合成为一张 UV 参考图。

50 帧逐像素中值作背景帧；每帧与背景的 RGB 欧氏距离超阈判为前景；
按时间顺序把前景叠到背景上，后帧覆盖前帧。中值与差分按水平条带计算，
避免 4K 尺寸下 float32 中间量吃满内存。
"""
import numpy as np

from python.annotation_preview import common as C

# 与水下相机无关的三台：两台高空俯视 + 一台 orbbec，快照时刻完全相同。
CAMERAS = ("overhead5", "overhead6", "orbbec_camera_1")
BAND_ROWS = 256                                     # 分带高度，压住 float32 峰值内存


def bands(height, band_rows):
    """把 [0, height) 切成 [(y0, y1)] 条带。"""
    step = max(1, int(band_rows))
    return [(y, min(y + step, height)) for y in range(0, height, step)]


def weighted_median(hist):
    """直方图加权下中位数；空直方图返回 None。"""
    total = int(hist.sum())
    if total == 0:
        return None
    return int(np.searchsorted(np.cumsum(hist), (total + 1) // 2))


def median_background(stack, band_rows=BAND_ROWS):
    """逐像素取时间轴中值，得到干净空池背景帧。"""
    _n, h, w, _c = stack.shape
    out = np.empty((h, w, 3), dtype=np.uint8)
    for y0, y1 in bands(h, band_rows):
        out[y0:y1] = np.median(stack[:, y0:y1].astype(np.float32), axis=0).astype(np.uint8)
    return out
```

- [ ] **Step 4: 运行测试确认前 6 个用例通过**

Run: `.venv/bin/python -m unittest tests.python.test_merge_overhead -v`
Expected: PASS，`Ran 6 tests` / `OK`

- [ ] **Step 5: 追加 median_background 与 merge_frames 的测试**

把 `tests/python/test_merge_overhead.py` 顶部的 import 改为：

```python
from python.annotation_preview.merge_overhead import (
    BAND_ROWS,
    CAMERAS,
    median_background,
    merge_frames,
    weighted_median,
)
```

在文件末尾追加：

```python
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 6: 实现 merge_frames**

在 `python/annotation_preview/merge_overhead.py` 末尾追加：

```python
def merge_frames(stack, background, thresh=C.DIST_THRESH, band_rows=BAND_ROWS):
    """按时间顺序把每帧前景叠到背景上（后帧覆盖前帧）。

    返回 (合成图, 每帧锚点)。锚点是该帧前景像素坐标的分量下中位数 (x, y)，
    由行/列直方图累加得到——直方图与条带切分无关，内存也只有 O(H + W)。
    无前景的帧锚点为 None。
    """
    n, h, w, _c = stack.shape
    merged = background.copy()
    base = background.astype(np.float32)
    row_hist = np.zeros((n, h), dtype=np.int64)
    col_hist = np.zeros((n, w), dtype=np.int64)
    limit = float(thresh) ** 2
    for y0, y1 in bands(h, band_rows):
        band_base = base[y0:y1]
        band_out = merged[y0:y1]                     # 基础切片是视图，写入直达 merged
        for i in range(n):
            frame = stack[i, y0:y1]
            dist2 = ((frame.astype(np.float32) - band_base) ** 2).sum(axis=2)
            mask = dist2 > limit
            if not mask.any():
                continue
            band_out[mask] = frame[mask]
            ys, xs = np.nonzero(mask)
            row_hist[i] += np.bincount(ys + y0, minlength=h)
            col_hist[i] += np.bincount(xs, minlength=w)
    anchors = []
    for i in range(n):
        x = weighted_median(col_hist[i])
        y = weighted_median(row_hist[i])
        anchors.append(None if x is None or y is None else (x, y))
    return merged, anchors
```

- [ ] **Step 7: 运行测试确认通过**

Run: `.venv/bin/python -m unittest tests.python.test_merge_overhead -v`
Expected: PASS，`Ran 13 tests` / `OK`

- [ ] **Step 8: 跑全量回归**

Run: `.venv/bin/python -m unittest discover -s tests/python -t .`
Expected: `Ran 95 tests` / `OK`

- [ ] **Step 9: 提交**

```bash
git add python/annotation_preview/merge_overhead.py tests/python/test_merge_overhead.py
git commit -m "feat(annotation): banded median background and foreground merge"
```

---

### Task 2: 帧号标注与图例带

给合成图叠帧号锚点，并在图下方追加一条 `f01 → 时间` 的图例带作兜底。不引入任何检测。

**Files:**
- Modify: `python/annotation_preview/merge_overhead.py`（在文件末尾追加）
- Modify: `tests/python/test_merge_overhead.py`（在 `if __name__` 之前追加）

**Interfaces:**
- Consumes: Task 1 的 `merge_frames` 返回的 `anchors: list[tuple[int, int] | None]`；`common.load_font`。
- Produces:
  - `LEGEND_PAD: int` = `8`
  - `snapshot_time_label(snapshot_id) -> str`：`"raw_1783480173576_15"` → `"11:09:33"`（本地时区）
  - `frame_color(index, total) -> tuple[int, int, int]`：HSV 色相均匀取值的 RGB
  - `annotate(merged, anchors, labels) -> np.ndarray`：`labels` 为 `[(frame_no, time_str)]`，出图高度 = 原高 + 图例带高，宽度不变

- [ ] **Step 1: 写失败的测试**

在 `tests/python/test_merge_overhead.py` 的 `if __name__ == "__main__":` 之前追加：

```python
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
```

同时把该文件顶部的 import 改为：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest tests.python.test_merge_overhead -v`
Expected: FAIL — `ImportError: cannot import name 'annotate'`

- [ ] **Step 3: 写实现**

在 `python/annotation_preview/merge_overhead.py` 顶部的 import 区补上：

```python
import colorsys
import datetime
import re

from PIL import Image, ImageDraw
```

在文件末尾追加：

```python
LEGEND_PAD = 8                                      # 图例带内边距


def snapshot_time_label(snapshot_id):
    """raw_<毫秒时间戳>_<序号> -> 本地时间 HH:MM:SS；解析失败原样返回。"""
    m = re.match(r"raw_(\d+)_", snapshot_id)
    if not m:
        return snapshot_id
    return datetime.datetime.fromtimestamp(int(m.group(1)) / 1000.0).strftime("%H:%M:%S")


def frame_color(index, total):
    """按帧序在色相环上均匀取色，便于看出时间方向。"""
    hue = (index % max(1, total)) / float(max(1, total))
    r, g, b = colorsys.hsv_to_rgb(hue, 0.95, 1.0)
    return (int(r * 255), int(g * 255), int(b * 255))


def annotate(merged, anchors, labels):
    """在合成图上标帧号，并在下方追加 f<NN> -> 时间 的图例带。

    锚点可能被池边人群拉偏，个别标签会落在非线位置；图例带是兜底，
    即使锚点不准也能按线的空间顺序对上编号。
    """
    h, w, _c = merged.shape
    total = len(labels)
    size = max(12, w // 120)
    font = C.load_font(size)
    line_h = size + 6
    swatch = size

    # 按最宽条目算列宽，列数取图宽装得下的最大值：50 条在 4K 下排 4 列，
    # 在低分辨率的 orbbec 上自动收窄，不会互相压字。
    entries = ["f%02d  %s" % (n, t) for n, t in labels]
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    text_w = max([int(measure.textlength(e, font=font)) for e in entries] or [size])
    col_w = swatch + LEGEND_PAD + text_w + 2 * LEGEND_PAD
    cols = max(1, min(total, w // col_w)) if total else 1
    rows = (total + cols - 1) // cols if total else 0
    legend_h = 2 * LEGEND_PAD + rows * line_h

    canvas = Image.new("RGB", (w, h + legend_h), (16, 16, 16))
    canvas.paste(Image.fromarray(merged), (0, 0))
    draw = ImageDraw.Draw(canvas)

    radius = max(3, size // 3)
    for i, entry in enumerate(entries):
        color = frame_color(i, total)
        anchor = anchors[i] if i < len(anchors) else None
        if anchor is not None:
            x, y = anchor
            draw.ellipse((x - radius, y - radius, x + radius, y + radius),
                         fill=color, outline=(0, 0, 0))
            # 图上只标帧号，时间放图例带；否则 50 个时间戳会糊成一片。
            draw.text((x + radius + 2, y - line_h), "f%02d" % labels[i][0],
                      fill=color, font=font, stroke_width=1, stroke_fill=(0, 0, 0))
        col, row = divmod(i, max(1, rows))
        lx = LEGEND_PAD + col * col_w
        ly = h + LEGEND_PAD + row * line_h
        draw.rectangle((lx, ly + 2, lx + swatch, ly + swatch), fill=color)
        draw.text((lx + swatch + LEGEND_PAD, ly), entry, fill=(235, 235, 235), font=font)
    return np.asarray(canvas, dtype=np.uint8)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m unittest tests.python.test_merge_overhead -v`
Expected: PASS，`Ran 22 tests` / `OK`

- [ ] **Step 5: 跑全量回归**

Run: `.venv/bin/python -m unittest discover -s tests/python -t .`
Expected: `Ran 104 tests` / `OK`

- [ ] **Step 6: 提交**

```bash
git add python/annotation_preview/merge_overhead.py tests/python/test_merge_overhead.py
git commit -m "feat(annotation): label merged frames with index and legend band"
```

---

### Task 3: 帧加载与逐相机编排

唯一接触文件系统的一层：枚举帧、解码进栈、校验尺寸、写三张 PNG。

**Files:**
- Modify: `python/annotation_preview/merge_overhead.py`（在文件末尾追加）
- Modify: `tests/python/test_merge_overhead.py`（在 `if __name__` 之前追加）

**Interfaces:**
- Consumes: Task 1 的 `median_background` / `merge_frames`；Task 2 的 `annotate` / `snapshot_time_label`；`common.frames_for_camera`、`common.OUTPUT_ROOT`。
- Produces:
  - `OUT_DIR: str` = `os.path.join(C.OUTPUT_ROOT, "overhead-merge")`
  - `FrameSizeError(Exception)`
  - `load_stack(paths, scale=1) -> np.ndarray`：出 `(N, H, W, 3) uint8`；尺寸不一致抛 `FrameSizeError`
  - `run_camera(cam, out_dir=OUT_DIR, thresh=C.DIST_THRESH, band_rows=BAND_ROWS, scale=1) -> list[str]`：返回写出的文件路径；零帧返回 `[]`

- [ ] **Step 1: 写失败的测试**

在 `tests/python/test_merge_overhead.py` 的 `if __name__ == "__main__":` 之前追加：

```python
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
```

同时把该文件顶部的 import 区改为：

```python
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
    median_background,
    merge_frames,
    run_camera,
    snapshot_time_label,
    weighted_median,
)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest tests.python.test_merge_overhead -v`
Expected: FAIL — `ImportError: cannot import name 'FrameSizeError'`

- [ ] **Step 3: 写实现**

在 `python/annotation_preview/merge_overhead.py` 的 import 区补上 `import os`，并在文件末尾追加：

```python
OUT_DIR = os.path.join(C.OUTPUT_ROOT, "overhead-merge")


class FrameSizeError(Exception):
    """同一相机各帧尺寸不一致；静默缩放会产出错误的参考图，所以直接失败。"""


def load_stack(paths, scale=1):
    """解码一组同尺寸图像进 (N, H, W, 3) uint8 栈；scale>1 时整数降采样。"""
    frames, size = [], None
    for path in paths:
        im = Image.open(path).convert("RGB")
        if size is None:
            size = im.size
        elif im.size != size:
            raise FrameSizeError(
                "帧尺寸不一致：%s 为 %dx%d，期望 %dx%d"
                % (os.path.basename(path), im.size[0], im.size[1], size[0], size[1]))
        if scale > 1:
            im = im.resize((im.width // scale, im.height // scale), Image.BILINEAR)
        frames.append(np.asarray(im, dtype=np.uint8))
    return np.stack(frames, axis=0)


def run_camera(cam, out_dir=OUT_DIR, thresh=C.DIST_THRESH, band_rows=BAND_ROWS, scale=1):
    """跑完一台相机：中值背景 -> 前景叠加 -> 标注，写出三张 PNG。"""
    frames = C.frames_for_camera(cam)
    if not frames:
        print("%-16s 无匹配帧，跳过" % cam)
        return []

    stack = load_stack([p for _sid, p in frames], scale=scale)
    print("%-16s frames=%d  %dx%d" % (cam, len(stack), stack.shape[2], stack.shape[1]))
    background = median_background(stack, band_rows=band_rows)
    merged, anchors = merge_frames(stack, background, thresh=thresh, band_rows=band_rows)
    labels = [(i + 1, snapshot_time_label(sid)) for i, (sid, _p) in enumerate(frames)]
    labeled = annotate(merged, anchors, labels)

    os.makedirs(out_dir, exist_ok=True)
    written = []
    for suffix, image in (("background", background),
                          ("merged", merged),
                          ("merged_labeled", labeled)):
        path = os.path.join(out_dir, "%s_%s.png" % (cam, suffix))
        Image.fromarray(image).save(path)
        written.append(path)
        print("  wrote %s" % path)
    return written
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m unittest tests.python.test_merge_overhead -v`
Expected: PASS，`Ran 28 tests` / `OK`

- [ ] **Step 5: 跑全量回归**

Run: `.venv/bin/python -m unittest discover -s tests/python -t .`
Expected: `Ran 110 tests` / `OK`

- [ ] **Step 6: 提交**

```bash
git add python/annotation_preview/merge_overhead.py tests/python/test_merge_overhead.py
git commit -m "feat(annotation): load snapshot stacks and orchestrate per-camera merge"
```

---

### Task 4: CLI 与 shell 入口

命令行参数、缺目录报错、`run_python.sh oh-merge` 子命令。

**Files:**
- Modify: `python/annotation_preview/merge_overhead.py`（在文件末尾追加）
- Modify: `tests/python/test_merge_overhead.py`（在 `if __name__` 之前追加）
- Modify: `scripts/run_python.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 3 的 `run_camera`、`OUT_DIR`、`CAMERAS`。
- Produces: `main(argv=None) -> None`

- [ ] **Step 1: 写失败的测试**

在 `tests/python/test_merge_overhead.py` 的 `if __name__ == "__main__":` 之前追加：

```python
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
```

同时把 import 里的 `run_camera,` 一行之后补上 `main,`（保持字母序：`load_stack, main, median_background`）。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/bin/python -m unittest tests.python.test_merge_overhead -v`
Expected: FAIL — `ImportError: cannot import name 'main'`

- [ ] **Step 3: 写实现**

在 `python/annotation_preview/merge_overhead.py` 的 import 区补上 `import argparse`，并在文件末尾追加：

```python
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cameras", nargs="+", default=list(CAMERAS),
                    help="要处理的相机（默认 %(default)s）")
    ap.add_argument("--thresh", type=float, default=C.DIST_THRESH,
                    help="前景判定的 RGB 距离阈值（默认 %(default)s）")
    ap.add_argument("--band-rows", type=int, default=BAND_ROWS,
                    help="分带高度，越小越省内存（默认 %(default)s）")
    ap.add_argument("--scale", type=int, default=1,
                    help="整数降采样倍数，仅调试提速用（默认 %(default)s）")
    ap.add_argument("--out-dir", default=OUT_DIR,
                    help="输出目录（默认 %(default)s）")
    args = ap.parse_args(argv)

    if not os.path.isdir(C.SNAP_DIR):
        raise SystemExit(
            "缺少快照目录：%s（请通过 ANNOTATION_PREVIEW_DATASET_ROOT 指向有效数据集）"
            % C.SNAP_DIR)

    total = 0
    for cam in args.cameras:
        total += len(run_camera(cam, out_dir=args.out_dir, thresh=args.thresh,
                                band_rows=args.band_rows, scale=args.scale))
    print("\n共写出 %d 个文件 -> %s" % (total, args.out_dir))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/bin/python -m unittest tests.python.test_merge_overhead -v`
Expected: PASS，`Ran 32 tests` / `OK`

- [ ] **Step 5: 加 shell 子命令**

在 `scripts/run_python.sh` 的 `cmd_keypoint()` 函数之后插入：

```bash
cmd_oh_merge() {
  "$PY" -m python.annotation_preview.merge_overhead "$@"
}
```

在 `usage()` 的 heredoc 里，`keypoint   Build COCO-17 person-crop HTML review page` 那行之后插入：

```text
  oh-merge   Merge each overhead/orbbec camera's snapshots into one UV reference
```

在文件顶部注释块的 `#   ./scripts/run_python.sh keypoint [--dataset-root PATH] [...]` 之后插入：

```bash
#   ./scripts/run_python.sh oh-merge [--cameras ...] [--scale N]
```

在 `case "$COMMAND" in` 的 `keypoint|kp) cmd_keypoint "$@" ;;` 之后插入：

```bash
  oh-merge) cmd_oh_merge "$@" ;;
```

- [ ] **Step 6: 用降采样跑通真实数据**

Run: `./scripts/run_python.sh oh-merge --scale 8`
Expected: 打印三台相机各 `frames=50`，共写出 9 个文件到 `outputs/annotation_preview/overhead-merge/`；`overhead5` 尺寸为 `480x270`（3840/8 × 2160/8），`orbbec_camera_1` 为 `160x90`。

- [ ] **Step 7: 提交**

```bash
git add python/annotation_preview/merge_overhead.py tests/python/test_merge_overhead.py scripts/run_python.sh
git commit -m "feat(annotation): add oh-merge CLI and shell entry point"
```

- [ ] **Step 8: 全分辨率产出并肉眼验收**

Run: `./scripts/run_python.sh oh-merge`
Expected: 三台相机全部成功；`overhead5_merged.png` 为 3840×2160，`orbbec_camera_1_merged.png` 为 1280×720。参考实测：单台 4K 相机约 21 秒、峰值 RSS 约 2.1 GiB。

打开 `outputs/annotation_preview/overhead-merge/overhead5_merged.png` 确认：背景是干净空池（无白线残留），图上能看到一组沿池长方向等间距排列的白线（实测确实浮出，池右侧另有 50 个时刻叠起来的一片人影，属预期）。若背景帧里仍有白线残影，说明该位置的线在超过半数帧里都存在，需回报而非私自调阈值。

- [ ] **Step 9: 更新 README**

在 `README.md` 的「处理流程」有序列表末尾追加一项：

```markdown
6. `./scripts/run_python.sh oh-merge` 把 `overhead5` / `overhead6` / `orbbec_camera_1` 各自全时段快照合成为一张 UV 参考图，输出到 `outputs/annotation_preview/overhead-merge/`。
```

- [ ] **Step 10: 最终全量回归并提交**

Run: `.venv/bin/python -m unittest discover -s tests/python -t .`
Expected: `Ran 114 tests` / `OK`

```bash
git add README.md
git commit -m "docs: document the oh-merge overhead reference command"
```



