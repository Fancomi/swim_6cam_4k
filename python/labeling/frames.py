#!/usr/bin/env python3
"""快照 → object-frames 产物的统一入口：整理、合成、标注、拼接。

把原来散在五个模块（organize_under / object_frames / snapshot_frames /
mask_merge / mask_grid）里的逻辑合并到这里，单一 CLI 子命令分发。
数据布局统一为 <数据集根>/<date>/snapshots/raw_*（每目录每相机一张 jpg），
产物统一到 <数据集根>/<date>/object-frames/。

子命令：
  organize    把所有相机的快照整理成每相机帧文件夹（f<NN>_<快照>__<原文件>.jpg）；
              对 underwater 16 相机额外跑中值背景差分筛选，产出 detections.csv /
              curated.csv（对齐旧数据口径）。
  auto_merge  自动合成（中值背景 + 差分前景叠加，流式分带、内存按条带封顶）。
              --camera 必填：逐相机合成，避免误把全部相机都跑。
  merge       手动合成（读 mask_label_project.json，mask 覆盖处取原帧、其余取
              中值背景）。处理工程里存在的所有相机；每块 mask 的中心标 f<帧ID>
              与泳道米数，单张图自己就能看出哪块来自哪帧。
  grid        仅 underwater：把 16 相机的 mask 合成图按 4×4 cat 拼成一张大图，
              每格标注相机 ID + 泳道米数（FBX 世界 X）。帧标签已在各相机的
              mask_merged 图上，这里不重复画。
  products    按 PRODUCTS 配方一次跑完四类标定产物（见下），产物名直接是交付名，
              不需要事后手工改名。--only 挑一类，--dry-run 只打印要跑什么。
  label       起浏览器 mask 标注器（转发 server.py）。

四类标定产物的配方写在 PRODUCTS 里（数据即文档），products 子命令照它执行：
  1 underwater  20260807 水下 16 相机 mask 合成，带 f<帧ID> + 泳道米数，再 4×4 拼接
  2 sixcam      Horizontal+Vertical 横竖合并的 6 相机拉线自动合成（zcam1-4 +
                overhead5/6），MAD 门控滤水花，产物落 Horizontal
  3 overhead    20260708 + Horizontal 融合的 overhead5/6 mask 合成，产物落 20260708
  4 entry       gemini/femto 各数据集单独 mask 合成（20260708 只有 orbbec_camera_1）

背景只有一份口径：全部快照帧的逐像素中值，三条链路（organize --write-background /
auto_merge / merge）产物内容一致，统一命名 <相机>_background.png，不再有
mask_background 与 median_background 两套名字。

米数口径不写死在代码里：录制事故（缺帧、重复帧）是**这批数据的属性**，放
<snapshots>/frame_meters.json（见 load_meter_spec），代码只读不猜。

合成纯函数（bands / median_background / merge_frames / load_stack）继续复用
merge_overhead.py，不复制实现。
"""
import argparse
import csv
import json
import os
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

from python.common.media import read_image, write_image
from python.common.paths import OUTPUTS, PROJECT_ROOT, dataset_root
from python.labeling import snapshots as S
from python.labeling.merge_overhead import (
    BAND_ROWS as MO_BAND_ROWS,
    FrameSizeError,
    load_stack,
    median_background,
    merge_frames,
)

# ---------------- 常量 ----------------
DATASET = dataset_root(
    "SWIM_UNDER_GRIDS_ROOT",
    "/Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-xlj-under-grids",
)
DEFAULT_DATE = "20260807"
DATE = DEFAULT_DATE                       # 与 mask_merge 的 DATE 同名兼容
# 相机分组：同一链路、同一处理逻辑。
UNDER_CAMERAS = tuple("underA%d" % i for i in range(16, 0, -1))   # 泳道左→右
OVERHEAD_CAMERAS = ("overhead5", "overhead6")
ENTRY_CAMERAS = ("xlj_aux_orbbec_femto_1", "gemini_camera_1")
ALL_CAMERAS = UNDER_CAMERAS + OVERHEAD_CAMERAS + ENTRY_CAMERAS

# 中值背景差分筛选（仅 underwater）口径，与旧数据一致。
DIST_THRESH = S.DIST_THRESH
CURATE_RATIO = 1.28
BAND_ROWS = 64

# grid 用的网格常量。
GRID_COLS = GRID_ROWS = 4
MESH_JSON = OUTPUTS / "underwater" / "all_mesh.json"
TEX_NAME = {i: "underA%d-grid.png" % i for i in range(1, 17)}

PROJECT_FILENAME = "mask_label_project.json"
SCHEMA = "mask-label/project-v1"

# 20260807 一批标定的四类产物配方。以前这些命令只写在文档里、靠人照抄重跑，
# 中间还有一步手工改名——现在配方是数据、products 子命令照它执行，可复现。
# 每条：kind 决定跑哪个子命令，其余键是该子命令的参数。
SIXCAM_DATES = ("20260807-6cam-Horizontal", "20260807-6cam-Vertical")
SIXCAM_CAMERAS = tuple("xlj_aux_zcam_%d" % i for i in range(1, 5)) + OVERHEAD_CAMERAS
PRODUCTS = {
    # 1. 水下 16 相机 mask 合成（带帧号 + 泳道米数）+ 4×4 拼接
    "underwater": (
        {"kind": "merge", "dates": ("20260807",), "cameras": UNDER_CAMERAS},
        {"kind": "grid", "date": "20260807"},
    ),
    # 2. 6 相机拉线自动合成：横竖两段当一段（拉线每帧位置不同，帧全要）
    "sixcam": tuple(
        {"kind": "auto_merge", "camera": cam, "dates": SIXCAM_DATES,
         "out_date": SIXCAM_DATES[0]}
        for cam in SIXCAM_CAMERAS
    ),
    # 3. overhead5/6 mask 合成：20260708 与 Horizontal 融合，产物落 20260708
    "overhead": (
        {"kind": "merge", "dates": ("20260807-6cam-Horizontal", "20260708"),
         "cameras": OVERHEAD_CAMERAS, "out_date": "20260708"},
    ),
    # 4. 入水机位 mask 合成：各数据集单独出，20260708 用旧相机名 orbbec_camera_1
    "entry": (
        {"kind": "merge", "dates": ("20260807",), "cameras": ENTRY_CAMERAS},
        {"kind": "merge", "dates": ("20260807-6cam-Horizontal",),
         "cameras": ENTRY_CAMERAS},
        {"kind": "merge", "dates": ("20260807-6cam-Vertical",),
         "cameras": ENTRY_CAMERAS},
        {"kind": "merge", "dates": ("20260708",), "cameras": ("orbbec_camera_1",)},
    ),
}


# ---------------- 路径 ----------------
def object_frames_root(date=DEFAULT_DATE):
    """产物根：<数据集根>/<date>/object-frames（与旧 20260708 平级）。"""
    return DATASET / str(date) / "object-frames"


def snapshots_dir(date=DEFAULT_DATE):
    """快照根：<数据集根>/<date>/snapshots。"""
    return DATASET / str(date) / "snapshots"


def frames_for_camera(camera, date=DEFAULT_DATE):
    """[(snapshot_id, path)] 一台相机在快照根下全部帧，按时间升序。

    直接扫本模块的 snapshots_dir（可 patch 测试），不依赖 snapshots 模块的
    全局根；目录名 raw_<ms>_<n> 字典序即时间序。"""
    found = []
    for directory in sorted(snapshots_dir(date).glob("raw_*")):
        if not directory.is_dir():
            continue
        hits = sorted(directory.glob("*__%s.jpg" % camera))
        if hits:
            found.append((directory.name, str(hits[0])))
    return found


# ---------------- 通用整理 ----------------
def organize_camera(camera, date=DEFAULT_DATE, out_root=None):
    """把一台相机全部快照复制成 <out_root>/<相机>/f<NN>_<快照名>__<原文件名>.jpg。

    复制而非符号链接：人工确认会把整个文件夹拷走，链接会断。f<NN> 按快照时间序
    编号；`__` 后保留原始文件名（含序号_stitch名__相机id），相机信息不丢。重复
    运行覆盖同名文件，幂等。
    """
    frames = frames_for_camera(camera, date=date)
    if not frames:
        return []
    if out_root is None:
        out_root = object_frames_root(date)
    out_dir = Path(out_root) / camera
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for index, (snapshot_id, src) in enumerate(frames, start=1):
        dst = out_dir / ("f%02d_%s__%s" % (index, snapshot_id, os.path.basename(src)))
        if not dst.exists() or os.path.getsize(dst) != os.path.getsize(src):
            shutil.copy2(src, dst)
        written.append(str(dst))
    return written


# ---------------- 流式自动合成（中值背景 + 差分前景） ----------------
def _decode(path):
    """解码一张帧为 RGB uint8 (H, W, 3)。"""
    from PIL import Image
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    return np.asarray(im, dtype=np.uint8)


def _decode_band(path, y0, y1):
    """解码一张帧的 [y0, y1) 水平条带，PIL crop 只解需要的行（省一半解码时间）。"""
    from PIL import Image
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    return np.asarray(im.crop((0, y0, im.width, y1)), dtype=np.uint8)


def _bands(height, band_rows):
    step = max(1, int(band_rows))
    return [(y, min(y + step, height)) for y in range(0, height, step)]


# 单条带装全部帧的内存预算。中值背景必须把该带的所有帧同时装进来才能取中值，
# 帧数翻倍内存就翻倍；分几次算不影响结果，所以宁可自动收窄也不要吃满内存。
MEM_BUDGET_BYTES = 512 * 1024 * 1024


def _fit_band_rows(band_rows, n_frames, width, height, budget=MEM_BUDGET_BYTES):
    """把 band_rows 收到内存预算内：帧数×带高×宽×3（uint8）≤ budget。

    返回不超过原值、且 >=1 的带高。帧数少时原值就够，直接返回。"""
    per_row = max(1, n_frames * width * 3)
    fit = max(1, int(budget // per_row))
    return max(1, min(int(band_rows), fit, int(height)))


def median_background_streaming(paths, band_rows=BAND_ROWS):
    """逐像素中值背景，内存按条带封顶；与 merge_overhead.median_background 逐位一致。

    band_rows 按 MEM_BUDGET_BYTES 自动收窄：中值要整条带装全部帧，帧数多时
    调用方未必记得调小，收窄只是多分几次算，结果不变。"""
    first = _decode(paths[0])
    height, width = first.shape[:2]
    band_rows = _fit_band_rows(band_rows, len(paths), width, height)
    out = np.empty((height, width, 3), dtype=np.uint8)
    for y0, y1 in _bands(height, band_rows):
        stack = np.stack([_decode_band(p, y0, y1) for p in paths])
        out[y0:y1] = np.median(stack, axis=0).astype(np.uint8)
    return out


def merge_frames_streaming(paths, background, thresh=DIST_THRESH,
                           band_rows=BAND_ROWS):
    """把每帧前景按时间序叠到背景上（后帧覆盖前帧），流式逐帧、峰值内存一帧。

    与 merge_overhead.merge_frames 逐位一致（单测断言），差别只在不装全栈。

    曾试过用逐像素 MAD 门控滤掉"每帧都在晃"的水花与灯光反射，实测无效：
    水花的 MAD 高（≈34 vs 拉线 ≈3）但偏离幅度也高（113 vs 95），所以
    `偏离 > MAD×k` 对两者同时成立——拉线与水花按同比例一起掉，没有哪个 k
    能只滤水花。要去水花得筛形状（拉线是长直线、水花是团块），不是筛时序统计量。
    """
    first = _decode(paths[0])
    height, width = first.shape[:2]
    merged = background.copy()
    base = background.astype(np.float32)
    limit = float(thresh) ** 2
    for path in paths:
        frame = _decode(path)
        for y0, y1 in _bands(height, band_rows):
            band, band_base = frame[y0:y1], base[y0:y1]
            mask = ((band.astype(np.float32) - band_base) ** 2).sum(axis=2) > limit
            if mask.any():
                merged[y0:y1][mask] = band[mask]
    return merged


def auto_merge_camera(camera, date=DEFAULT_DATE, out_root=None, thresh=DIST_THRESH,
                      band_rows=BAND_ROWS, merge_step=1, dates=None):
    """自动合成一台相机：中值背景 + 差分前景叠加，输出 <相机>_{background,merged}.png。

    dates 给出时跨多个数据集收集该相机的全部帧（按各自快照时间序拼接），用于
    把时间连续的几段（如 6cam-Horizontal + 6cam-Vertical）当成一段重新合成；
    单 date 时不传。帧序 = 各数据集帧首尾相接（数据集间时间连续才正确）。
    """
    if dates is None:
        dates = [date]
    if out_root is None:
        out_root = object_frames_root(dates[0])
    frames = []
    for d in dates:
        frames.extend(frames_for_camera(camera, date=d))
    if not frames:
        print("%-24s 无匹配帧，跳过" % camera)
        return []
    sampled = [p for _sid, p in frames][::merge_step]
    background = median_background_streaming(sampled, band_rows=band_rows)
    merged = merge_frames_streaming(sampled, background, thresh=thresh,
                                    band_rows=band_rows)
    height, width = background.shape[:2]
    n_total = len(frames)
    print("%-24s frames=%d（合成用 %d 帧）  %dx%d"
          % (camera, n_total, len(sampled), width, height))
    written = []
    for suffix, image in (("background", background), ("merged", merged)):
        path = write_image(Path(out_root) / ("%s_%s.png" % (camera, suffix)),
                           image[:, :, ::-1], "auto-merge")
        written.append(str(path))
        print("  wrote %s" % path)
    return written


# ---------------- 手动合成（mask 前景 + 中值背景） ----------------
class ProjectError(RuntimeError):
    """工程文件缺失或格式不符；消息直接面向用户。"""


def load_project(path, schema=SCHEMA):
    """读 mask_label_project.json，校验 schema，返回 cameras 字典。"""
    path = Path(path)
    if not path.is_file():
        raise ProjectError("缺少工程文件：%s" % path)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ProjectError("工程文件不是合法 JSON：%s：%s" % (path, exc)) from None
    if doc.get("schema") != schema:
        raise ProjectError("工程 schema 不符：期望 %s，实际 %s"
                           % (schema, doc.get("schema")))
    cameras = doc.get("cameras")
    if not isinstance(cameras, dict):
        raise ProjectError("工程缺少 cameras 对象")
    return cameras


def rasterize_strokes(strokes, width, height):
    """把一组胶囊笔画光栅化成 (height, width) bool 前景 mask（cv2 实心，与标注器一致）。"""
    mask = np.zeros((height, width), dtype=np.uint8)
    for s in strokes:
        x1, y1 = float(s["x1"]), float(s["y1"])
        x2, y2 = float(s["x2"]), float(s["y2"])
        r = max(0.0, float(s["r"]))
        if r < 0.5:
            continue
        p1, p2 = (round(x1), round(y1)), (round(x2), round(y2))
        cv2.circle(mask, p1, int(round(r)), 1, thickness=-1, lineType=cv2.LINE_8)
        cv2.circle(mask, p2, int(round(r)), 1, thickness=-1, lineType=cv2.LINE_8)
        cv2.line(mask, p1, p2, 1, thickness=max(1, int(round(2 * r))),
                 lineType=cv2.LINE_8)
    return mask.astype(bool)


def _resolve_images(project_cam, snapshots_roots):
    """工程 image 相对路径 → (snapshot_id, 绝对路径, 该帧笔画)。按文件名反查快照目录。

    snapshots_roots 可以是单个目录或目录列表：跨数据集合成时工程帧分散在多个
    数据集的 snapshots 下。匹配**优先用 image 里记录的目录名**（raw_<ms>_<n>）
    在所有根里精确找 `根/目录名/文件名`——glob 兜底会误命中同名文件（不同
    数据集的帧文件名相同，如 1_stitch__under-overhead-xlj__overhead5.jpg），
    把 A 数据集的帧错配成 B 数据集的。全部根都试过 exact 后仍无命中才走 glob。
    """
    roots = [Path(r) for r in (snapshots_roots if isinstance(snapshots_roots, (list, tuple))
                               else [snapshots_roots])]
    resolved = []
    for f in project_cam:
        image = f.get("image")
        if not image:
            continue
        rel, fname = Path(image), Path(image).name
        # 1) 精确匹配：image 记录的 raw_xxx 目录名，逐个根找
        hits = [snap_root / rel.parent / fname for snap_root in roots
                if (snap_root / rel.parent / fname).is_file()]
        # 2) 兜底：目录名对不上（工程可能是旧布局），按文件名扫 raw_* 目录
        if not hits:
            for snap_root in roots:
                hits = sorted(snap_root.glob("raw_*/*" + fname))
                if hits:
                    break
        if not hits:
            raise ProjectError("工程引用的帧找不到：%s（在 %s 下）"
                               % (image, "、".join(str(r) for r in roots)))
        resolved.append((hits[0].parent.name, str(hits[0]), f.get("strokes") or []))
    return resolved


def merge_camera(camera, project_cam, snapshots_root, out_dir,
                 band_rows=MO_BAND_ROWS, bg_paths=None, meters=None,
                 with_meters=True, annotate=True, merged_suffix="mask_merged"):
    """手动合成一台相机：中值背景 -> 逐帧按该帧 mask 叠前景，可选标帧 ID/米数。

    背景一律用该相机**全部帧**的逐像素中值（bg_paths），与 organize/auto_merge
    的背景逐位一致——三条链路进来产物内容相同，所以统一命名 <相机>_background.png。
    工程帧只决定前景（哪帧的哪块保留）。

    snapshots_root 可以是单个目录或列表（跨数据集：工程帧分散在多个快照根下）。
    每帧恰一个 mask（工程每帧一个 stroke、一个唯一横向位置）。标注控制：
      annotate=False     完全不标（入水机位 gemini/femto 不要帧号/米数）
      annotate=True  + with_meters=True   标 `f<帧ID> <米数>`（水下 16 相机）
      annotate=True  + with_meters=False  只标 `f<帧ID>`

    merged_suffix 决定合成图叫什么：水下走 grid 拼接读 `_mask_merged`（默认），
    overhead / 入水机位的交付名是 `_merged`——以前靠人手 mv 改名，现在参数化，
    产物名进代码不再需要事后重命名。返回 (背景路径, 合成路径)。
    """
    entries = _resolve_images(project_cam, snapshots_root)
    if not entries:
        raise ProjectError("该相机工程里没有可解析的帧")
    # 一帧多笔时后面的笔画会被合成但拿不到标签（_annotate_masks 只标一个位置），
    # 静默漏标比报错更难查，所以出声提醒。
    extra = [f.get("frame_index") for f in project_cam
             if len(f.get("strokes") or []) > 1]
    if extra:
        print("  ! %s 有 %d 帧画了多笔（f%s…），标签只标第一笔"
              % (camera, len(extra), extra[0]))
    fg_paths = [p for _sid, p, _st in entries]
    stack = load_stack(fg_paths)
    h, w = stack.shape[1], stack.shape[2]
    # 背景取全帧中值（口径与 organize/auto_merge 一致）；未给则退回工程帧。
    background = (median_background_streaming(bg_paths, band_rows=BAND_ROWS)
                  if bg_paths else median_background(stack, band_rows=band_rows))
    merged = background.copy()
    labels = []
    for frame, (snapshot_id, _p, strokes) in zip(stack, entries):
        if not strokes:
            continue
        mask = rasterize_strokes(strokes, w, h)
        merged[mask] = frame[mask]
        # 帧 ID 用 snapshot_id 匹配工程条目：同一相机在不同快照目录里的文件名
        # 相同（18_stitch__under-xlj-all__underA1.jpg），basename 匹配会全命中
        # 第一条；snapshot_id（raw_<ms>_<n>）才是每帧唯一的。
        labels.append((_frame_index_of(project_cam, snapshot_id), strokes))
    if annotate:
        _annotate_masks(merged, labels, meters or frame_meters(),
                        with_meters=with_meters)
    out_dir = Path(out_dir)
    paths = []
    for suffix, image in (("background", background), (merged_suffix, merged)):
        path = write_image(out_dir / ("%s_%s.png" % (camera, suffix)),
                           image[:, :, ::-1], "mask-merge")
        paths.append(str(path))
    return paths


def _frame_index_of(project_cam, snapshot_id):
    """从工程里找这条快照对应的 frame_index（找不到返回 0）。

    用 snapshot_id（raw_<ms>_<n>）匹配工程条目的 snapshot_id 字段——同一相机
    在所有快照里的文件名相同，basename 匹配会全部命中第一条，frame_index 就
    全错。"""
    for f in project_cam:
        if f.get("snapshot_id") == snapshot_id:
            return int(f.get("frame_index", 0))
    return 0


METER_SPEC_FILENAME = "frame_meters.json"
METER_SPEC_SCHEMA = "frame-meters/v1"


def load_meter_spec(path):
    """读帧→米数口径（sidecar）。文件不存在返回 None，表示用等距默认。

    口径不写在代码里：录制事故（缺帧、重复帧）是**这批数据的属性**，写死在
    函数里换一批数据就静默标错，而米数是下游 FBX 对齐的依据，错了很难发现。
    所以放工程文件旁边的 frame_meters.json：

        {"schema": "frame-meters/v1",
         "start": 0.5, "step": 0.5,
         "gaps": [28],        # 该帧之后缺一帧，米数跳过一格（补位）
         "skip": [35]}        # 该帧与前一帧重复，不标米数

    gaps / skip 都用帧号（frame_index）。缺字段按默认：start=step=0.5、无异常。
    """
    path = Path(path)
    if not path.is_file():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ProjectError("米数口径不是合法 JSON：%s：%s" % (path, exc)) from None
    if doc.get("schema") != METER_SPEC_SCHEMA:
        raise ProjectError("米数口径 schema 不符：期望 %s，实际 %s"
                           % (METER_SPEC_SCHEMA, doc.get("schema")))
    return doc


def frame_meters(spec=None, n_frames=None, overrides=None):
    """每帧对应的泳道米数：f1=start，逐帧 +step，按 spec 处理录制异常。

    泳者从 A1 端（最右）游向 A16 端（最左），每帧一个 mask、一个唯一横向位置，
    米数不依赖 UV 几何换算。spec（见 load_meter_spec）声明两类录制异常：

      gaps=[n]  第 n 帧之后缺一帧：米数多跳一格（那一格是没采到的位置），
                所以 f(n+1) 比 f(n) 多 2×step 而不是 1×step。
      skip=[n]  第 n 帧与前一帧采到同一时刻：米数置 None 表示**不标**，且
                不占位——下一帧接着前一帧继续递增。

    n_frames 缺省按 spec 的 n_frames，再缺省 51（旧数据帧数）；调用方通常从
    工程里的最大 frame_index 传进来，避免帧数写死。
    overrides: {frame_index: 米数|None} 最后覆盖，用于临时纠一两帧。
    """
    spec = spec or {}
    start = float(spec.get("start", 0.5))
    step = float(spec.get("step", 0.5))
    gaps = set(int(g) for g in spec.get("gaps", ()))
    skip = set(int(s) for s in spec.get("skip", ()))
    if n_frames is None:
        n_frames = int(spec.get("n_frames", 51))

    meters, m = {}, start
    for f in range(1, int(n_frames) + 1):
        if f in skip:
            meters[f] = None          # 重复帧不标、不占位
            continue
        meters[f] = round(m, 1)
        m += step * (2 if f in gaps else 1)   # gaps 之后多跳一格
    if overrides:
        meters.update({int(k): (float(v) if v is not None else None)
                       for k, v in overrides.items()})
    return meters


def _annotate_masks(image, labels, meters, with_meters=True):
    """在每帧 mask 上方标帧 ID（可选米数），每帧恰一个标签，尽量不盖 mask。

    每帧只标一个标签：工程里每帧一个 stroke（一个 mask、一个唯一横向位置）。
    with_meters=True 时标 `f<帧ID> <米数>`（meters[frame_index] 为 None 的帧，
    如 f35 与 f34 重合，不标）；False 时只标 `f<帧ID>`（入水机位不给米数）。
    标签放在 mask 顶边之上、按 x 排序做矩形避让，撞了往下挪。
    labels: [(frame_index, strokes)]。
    """
    items = []
    for frame_index, strokes in labels:
        if not strokes:
            continue
        if with_meters and meters.get(frame_index) is None:
            continue
        s = strokes[0]
        top = min(float(s["y1"]), float(s["y2"])) - float(s["r"])
        cx = (float(s["x1"]) + float(s["x2"])) / 2.0
        if with_meters:
            text = "f%02d %.1fm" % (frame_index, meters[frame_index])
        else:
            text = "f%02d" % frame_index
        items.append((cx, top, text))
    items.sort(key=lambda it: it[0])
    placed = []
    for cx, top, text in items:
        box = _place_label(image, text, int(cx), int(top), placed)
        if box:
            placed.append(box)


def _place_label(image, text, cx, cy, placed, line=15):
    """在 (cx, cy) 附近找一个不与 placed 相交的位置画标签，返回占用矩形。

    先试笔画中心正上方，撞了就逐行下移（最多 12 行，超出就放弃这条），避免
    标签互相糊住。返回 None 表示没画。
    """
    font, scale, thick = cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1
    (tw, th), _ = cv2.getTextSize(text, font, scale, thick)
    pad = 2
    bw, bh = tw + 2 * pad, th + 2 * pad
    h, w = image.shape[:2]
    if bw >= w or bh >= h:
        return None
    x = max(0, min(cx - bw // 2, w - bw - 1))
    for step in range(12):
        y = cy - bh - 6 + step * (bh + 2)
        y = max(0, min(y, h - bh - 1))
        if all(not (x < px + pw and px < x + bw and y < py + ph and py < y + bh)
               for px, py, pw, ph in placed):
            cv2.rectangle(image, (x, y), (x + bw, y + bh), (10, 10, 10),
                          thickness=-1)
            cv2.putText(image, text, (x + pad, y + th + pad), font, scale,
                        (255, 220, 90), thick, cv2.LINE_AA)
            return (x, y, bw, bh)
    return None


# ---------------- 差分筛选（仅 underwater，organize 时额外跑） ----------------
def _compute_scores(paths, dist_thresh=DIST_THRESH, band_rows=BAND_ROWS):
    """流式分带算中值背景与每帧差分占比，返回 (background, scores)。"""
    first = read_image(paths[0])
    height, width = first.shape[:2]
    n = len(paths)
    limit = float(dist_thresh) ** 2
    background = np.empty((height, width, 3), dtype=np.uint8)
    counts = np.zeros(n, dtype=np.int64)
    for y0, y1 in _bands(height, band_rows):
        stack = np.stack([read_image(p)[y0:y1] for p in paths])
        band_bg = np.median(stack.astype(np.float32), axis=0).astype(np.uint8)
        background[y0:y1] = band_bg
        base = band_bg.astype(np.float32)
        diff2 = ((stack.astype(np.float32) - base) ** 2).sum(axis=3)
        counts += (diff2 > limit).sum(axis=(1, 2))
    return background, counts / float(height * width)


def screen_underwater(date=DEFAULT_DATE, out_root=None, band_rows=BAND_ROWS,
                      ratio=CURATE_RATIO, write_background=False):
    """水下 16 相机：整理 + 中值差分筛选，写 detections.csv / curated.csv。

    与旧 20260708 口径一致：score_frac_gt40 = 超阈像素占比、threshold =
    cam_median × ratio、is_object = score > threshold。返回 (n_frames, n_curated)。
    """
    if out_root is None:
        out_root = object_frames_root(date)
    out_root = Path(out_root)
    all_rows = []
    total_frames = total_curated = 0
    for cam in UNDER_CAMERAS:
        frames = frames_for_camera(cam, date=date)
        if not frames:
            print("%-8s 无匹配帧，跳过" % cam)
            continue
        cam_dir = out_root / cam
        cam_dir.mkdir(parents=True, exist_ok=True)
        records = []
        for index, (snapshot_id, src) in enumerate(frames, start=1):
            orig = os.path.basename(src)
            dst = cam_dir / ("f%02d_%s__%s" % (index, snapshot_id, orig))
            if not dst.exists() or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copy2(src, dst)
            records.append({"snapshot_id": snapshot_id, "source_path": src,
                            "frame_index": index, "filename": orig})
        background, scores = _compute_scores([r["source_path"] for r in records],
                                             band_rows=band_rows)
        if write_background:
            write_image(out_root / ("%s_background.png" % cam),
                        background, "median background")
        cam_median = float(np.median(scores))
        threshold = cam_median * ratio
        rows = [{"camera": cam, "frame_index": r["frame_index"],
                 "snapshot_id": r["snapshot_id"],
                 "score_frac_gt40": "%.5f" % s, "cam_median": "%.5f" % cam_median,
                 "threshold": "%.5f" % threshold,
                 "is_object": 1 if s > threshold else 0, "filename": r["filename"]}
                for r, s in zip(records, scores)]
        n_sel = sum(r["is_object"] for r in rows)
        total_frames += len(records)
        total_curated += n_sel
        print("%-8s frames=%3d  选中 %2d/%-3d  cam_median=%.4f threshold=%.4f"
              % (cam, len(records), n_sel, len(records), cam_median, threshold))
        all_rows.extend(rows)
    out_root.mkdir(parents=True, exist_ok=True)
    cols = ["camera", "frame_index", "snapshot_id", "score_frac_gt40",
            "cam_median", "threshold", "is_object", "filename"]
    with open(out_root / "detections.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(all_rows)
    with open(out_root / "curated.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows([r for r in all_rows if r["is_object"]])
    print("\n水下 %d 相机：%d 帧，精选 %d 帧（%.1f%%）-> %s"
          % (len(UNDER_CAMERAS), total_frames, total_curated,
             100.0 * total_curated / max(1, total_frames), out_root))
    return total_frames, total_curated


# ---------------- 4×4 拼接（仅 underwater） ----------------
class MeshJsonError(RuntimeError):
    """all_mesh.json 缺失或结构不符；拼接需要它来定米数。"""


def camera_meters(mesh_json=MESH_JSON):
    """读 FBX 网格，返回 {相机: 泳道相对米数}（0m 在 underA16 端，25m 满泳道）。

    FBX 世界单位是米；每块平面中心 X 平移使泳道左端为 0m。缺相机抛错——米数
    标错比不标更糟。
    """
    if not Path(mesh_json).is_file():
        raise MeshJsonError("缺少 %s（先跑 ./scripts/run_stitch.sh underwater extract）"
                            % mesh_json)
    doc = json.loads(Path(mesh_json).read_text(encoding="utf-8"))
    centers, lane_min = {}, None
    for mesh in doc.get("meshes", []):
        tex = mesh.get("texture_basename", "")
        cam = next((c for c, t in TEX_NAME.items() if t == tex), None)
        if cam is None:
            continue
        xs = [v["pos"][0] for tri in mesh.get("triangles", []) for v in tri]
        if not xs:
            continue
        centers["underA%d" % cam] = (min(xs) + max(xs)) / 2.0
        lane_min = min(lane_min, min(xs)) if lane_min is not None else min(xs)
    missing = [c for c in UNDER_CAMERAS if c not in centers]
    if missing:
        raise MeshJsonError("mesh.json 缺平面：%s" % ", ".join(missing))
    return {cam: (cx - lane_min) for cam, cx in centers.items()}



def stitch_4x4(input_dir, out_path, meters, date=DEFAULT_DATE,
               cameras=UNDER_CAMERAS):
    """把 16 相机 mask 合成图按 4×4 cat 拼接，每格标注相机 ID + 米数。

    每帧 mask 的帧 ID/米数已由 merge 画在各相机的 mask_merged 图上，这里只标
    每格的相机 ID + 平面米数，不再重复画帧标签。缺图格子留淡底 + 仍标相机 ID。
    返回 (输出路径, 每格信息)。
    """
    input_dir = Path(input_dir)
    tiles, info = [], []
    for cam in cameras:
        p = input_dir / ("%s_mask_merged.png" % cam)
        m = meters.get(cam)
        if p.is_file():
            img = read_image(str(p))
            tiles.append(img)
            info.append({"cam": cam, "state": "有图", "meters": m,
                         "shape": img.shape})
        else:
            tiles.append(None)
            info.append({"cam": cam, "state": "缺图", "meters": m, "shape": None})

    have = [t for t in tiles if t is not None]
    th = max((t.shape[0] for t in have), default=1)
    tw = max((t.shape[1] for t in have), default=1)
    label_h = 24
    cell_w, cell_h = tw + 2, th + 2 + label_h
    canvas = np.zeros((cell_h * GRID_ROWS, cell_w * GRID_COLS, 3), dtype=np.uint8)

    for idx, cam in enumerate(cameras):
        r, c = divmod(idx, GRID_COLS)
        y0, x0 = r * cell_h, c * cell_w
        cv2.rectangle(canvas, (x0, y0), (x0 + cell_w - 1, y0 + label_h - 1),
                      (28, 32, 40), thickness=-1)
        tile = tiles[idx]
        if tile is not None:
            h, w = tile.shape[:2]
            y1, x1 = y0 + label_h + (cell_h - label_h - h) // 2, x0 + (cell_w - w) // 2
            canvas[y1:y1 + h, x1:x1 + w] = tile
        m = meters.get(cam)
        label = cam + ("  %.1fm" % m if m is not None else "")
        _put_label(canvas, label, x0 + 2, y0 + (label_h - 15) // 2, bg=(28, 32, 40))

    write_image(out_path, canvas, "mask-grid")
    return str(out_path), info


def _put_label(canvas, text, x, y, bg=(10, 10, 10), fg=(255, 255, 255)):
    """在 (x,y) 画文字 + 底色条；越界则夹进画布，装不下就整条不画。

    装不下直接跳过（而不是夹到 0,0 铺满）：小图上一条标签能盖掉整幅内容。"""
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale, thick = 0.5, 1
    (tw, th), _ = cv2.getTextSize(text, font, scale, thick)
    pad = 3
    h, w = canvas.shape[:2]
    box_w, box_h = tw + 2 * pad, th + 2 * pad
    if box_w >= w or box_h >= h:
        return
    x = max(0, min(x, w - box_w - 1))
    y = max(0, min(y, h - box_h - 1))
    cv2.rectangle(canvas, (x, y), (x + box_w, y + box_h), bg, thickness=-1)
    cv2.putText(canvas, text, (x + pad, y + th + pad), font, scale, fg,
                thick, cv2.LINE_AA)


# ---------------- CLI ----------------
def _parser():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("organize", help="整理所有相机；水下额外差分筛选")
    p.add_argument("--date", default=DEFAULT_DATE)
    p.add_argument("--cameras", nargs="+", default=None,
                   help="指定相机子集（默认全部）")
    p.add_argument("--out-root", default=None)
    p.add_argument("--band-rows", type=int, default=BAND_ROWS)
    p.add_argument("--ratio", type=float, default=CURATE_RATIO)
    p.add_argument("--write-background", action="store_true",
                   help="额外写 <相机>_background.png 中值背景图")
    p.set_defaults(func=cmd_organize)

    p = sub.add_parser("auto_merge", help="自动合成（中值+差分），--camera 必填")
    p.add_argument("--camera", required=True, help="要合成的相机")
    p.add_argument("--date", default=DEFAULT_DATE,
                   help="单数据集日期（与 --dates 互斥）")
    p.add_argument("--dates", nargs="+", default=None,
                   help="跨数据集合并：按顺序把多个日期根下该相机的帧当一段合成")
    p.add_argument("--out-root", default=None)
    p.add_argument("--thresh", type=float, default=DIST_THRESH)
    p.add_argument("--band-rows", type=int, default=BAND_ROWS)
    p.add_argument("--merge-step", type=int, default=1)
    p.set_defaults(func=cmd_auto_merge)

    p = sub.add_parser("merge", help="手动合成（mask 前景+中值背景），处理所有相机")
    p.add_argument("--project", default=None)
    p.add_argument("--date", default=DEFAULT_DATE)
    p.add_argument("--dates", nargs="+", default=None,
                   help="跨数据集合成：读各数据集工程、同名相机帧按时间合并重编号")
    p.add_argument("--root", default=str(DATASET))
    p.add_argument("--snapshots", default=None)
    p.add_argument("--cameras", nargs="+", default=None,
                   help="相机子集（默认工程里有的全部）")
    p.add_argument("--out-root", default=None)
    p.add_argument("--band-rows", type=int, default=MO_BAND_ROWS)
    p.add_argument("--meter-spec", default=None,
                   help="帧→米数口径 json（默认 <snapshots>/frame_meters.json）；"
                        "缺省文件不存在则按等距 f1=0.5m 每帧 +0.5m")
    p.add_argument("--meter-overrides", default=None,
                   help="临时纠正个别帧，如 '28:14.5,34:17.0'；长期口径写进 --meter-spec")
    p.add_argument("--merged-suffix", default=None,
                   help="合成图后缀（默认水下 mask_merged 给 grid 读、其余 merged）")
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("grid", help="仅水下：16 相机 4×4 拼接")
    p.add_argument("--date", default=DEFAULT_DATE)
    p.add_argument("--input-dir", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--mesh-json", default=str(MESH_JSON))
    p.set_defaults(func=cmd_grid)

    p = sub.add_parser("products",
                       help="按 PRODUCTS 配方一次跑完四类标定产物")
    p.add_argument("--only", choices=sorted(PRODUCTS), default=None,
                   help="只跑其中一类（默认全部）")
    p.add_argument("--dry-run", action="store_true", help="只打印要跑什么")
    p.add_argument("--date", default=DEFAULT_DATE, help="配方没写日期时的兜底")
    p.set_defaults(func=cmd_products)

    p = sub.add_parser("label", help="起浏览器 mask 标注器")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_label)
    return ap


def cmd_organize(args):
    snap = snapshots_dir(args.date)
    if not snap.is_dir():
        raise SystemExit("缺少快照目录：%s（请用 SWIM_UNDER_GRIDS_ROOT 指向数据集）" % snap)
    out_root = Path(args.out_root) if args.out_root else object_frames_root(args.date)
    cams = tuple(args.cameras) if args.cameras else ALL_CAMERAS
    # underwater 单独走筛选（含整理）；其他相机只整理
    under_given = [c for c in cams if c.startswith("underA")]
    if under_given and set(under_given) == set(UNDER_CAMERAS):
        screen_underwater(date=args.date, out_root=out_root,
                          band_rows=args.band_rows, ratio=args.ratio,
                          write_background=args.write_background)
    else:
        for cam in cams:
            n = len(organize_camera(cam, date=args.date, out_root=out_root))
            print("%-24s frames=%d" % (cam, n))


def cmd_auto_merge(args):
    dates = args.dates if args.dates else [args.date]
    # 多数据集时产物根取第一个数据集（合并结果归到起始段所在日期）。
    snap = snapshots_dir(dates[0])
    if not snap.is_dir():
        raise SystemExit("缺少快照目录：%s" % snap)
    out_root = Path(args.out_root) if args.out_root else object_frames_root(dates[0])
    written = auto_merge_camera(args.camera, date=args.date, out_root=out_root,
                                thresh=args.thresh, band_rows=args.band_rows,
                                merge_step=args.merge_step, dates=dates)
    print("\n共写出 %d 个文件 -> %s" % (len(written), out_root))


def _find_project(snap_root, date):
    """定位工程文件：优先 snapshots/ 内（mask_labeler 写回的落点），退回日期根。

    mask_labeler 把工程写到所选 snapshots 目录里（File System Access 写回所选
    根），但早期版本也存过日期根下，两个位置都找。"""
    for cand in (Path(snap_root) / PROJECT_FILENAME,
                 Path(snap_root).parent / PROJECT_FILENAME):
        if cand.is_file():
            return cand
    return Path(snap_root) / PROJECT_FILENAME


def _date_of_snapshot(snapshot_id, dates):
    """snapshot_id（raw_<ms>_<n>）属于哪个数据集。

    快照目录名在各数据集间不重复（时间戳不同），按目录存在性判断；找不到时
    回退到第一个数据集。用于跨数据集帧按 --dates 顺序分组排序。"""
    for d in dates:
        if (snapshots_dir(d) / snapshot_id).is_dir():
            return d
    return dates[0] if dates else DEFAULT_DATE


def cmd_merge(args):
    dates = args.dates if args.dates else [args.date]
    # 跨数据集时工程取第一个数据集（合并前景帧来自各数据集对应快照）。
    snap_root = Path(args.snapshots) if args.snapshots else \
        snapshots_dir(dates[0])
    if not snap_root.is_dir():
        raise SystemExit("缺少快照目录：%s" % snap_root)
    project = Path(args.project) if args.project else \
        _find_project(snap_root, dates[0])
    if not project.is_file():
        raise SystemExit("缺少工程文件：%s（请先跑 label 画保留区域）" % project)
    cameras = load_project(project)
    snap_roots = [snap_root]
    # 跨数据集（--dates）：读各数据集的工程、把同名相机的帧按数据集顺序合并，
    # 组内按时间序、组间按 --dates 传入顺序（H → V → 20260807 即后叠在上层）。
    # 跨数据集（--dates 给了两个以上）：读各数据集的工程、把同名相机的帧按数据集
    # 顺序合并，组内按时间序、组间按 --dates 传入顺序（后叠在上层）。
    # 只有真跨数据集才重编号——各批的 frame_index 各自从 1 起，合并后必须统一；
    # 单数据集时工程里的 frame_index 就是该相机的全局帧号（如水下 A11 是 f27~f36），
    # 重编号会把它压成 f01~f10，米数跟着全错。
    if len(dates) > 1:
        for d in dates[1:]:
            p2 = Path(args.project) if args.project else \
                _find_project(snapshots_dir(d), d)
            if not p2.is_file():
                continue
            snap_roots.append(snapshots_dir(d))
            for cam, frames in load_project(p2).items():
                cameras.setdefault(cam, []).extend(frames)
        for cam in cameras:
            order = {d: i for i, d in enumerate(dates)}
            cameras[cam].sort(key=lambda f: (order.get(
                _date_of_snapshot(f["snapshot_id"], dates), 0), f["snapshot_id"]))
            for i, f in enumerate(cameras[cam], 1):
                f["frame_index"] = i      # 跨数据集后按叠加顺序重新编号
    out_root = Path(args.out_root) if args.out_root else object_frames_root(dates[0])
    cams = tuple(args.cameras) if args.cameras else \
        [c for c in cameras if cameras.get(c)]
    overrides = None
    if args.meter_overrides:
        # '28:14.5,34:17.0' -> {28: 14.5, 34: 17.0}
        overrides = dict(pair.split(":") for pair in
                         args.meter_overrides.replace(" ", "").split(",") if pair)
    # 米数口径来自数据侧 sidecar（frame_meters.json），不写死在代码里；
    # 帧数按工程里的最大 frame_index，避免换批数据后帧数不符还静默出表。
    spec = load_meter_spec(Path(args.meter_spec) if args.meter_spec
                           else snap_root / METER_SPEC_FILENAME)
    n_frames = max((f.get("frame_index", 0)
                    for frames in cameras.values() for f in frames), default=0)
    meters = frame_meters(spec, n_frames=n_frames or None, overrides=overrides)
    total = 0
    try:
        for cam in cams:
            project_cam = cameras.get(cam)
            if not project_cam:
                print("%-24s 工程里没有该相机，跳过" % cam)
                continue
            # 背景口径与 organize/auto_merge 一致：该相机全部快照帧的中值；
            # 跨数据集时把各数据集帧首尾相接当一段（帧数可能不同，尺寸需一致）。
            bg_paths = [p for d in dates
                        for _sid, p in frames_for_camera(cam, date=d)]
            # 水下 16 相机标 f<帧ID>+米数；其余相机（overhead/gemini/femto）
            # 完全不标——入水机位不需要帧号/米数，overhead 混合图也不标。
            # 产物名同理按链路分：水下留 _mask_merged 给 grid 拼接读，其余直接
            # 出交付名 _merged（--merged-suffix 可覆盖）。
            is_under = cam.startswith("underA")
            suffix = args.merged_suffix or ("mask_merged" if is_under else "merged")
            paths = merge_camera(cam, project_cam, snap_roots, out_root,
                                 band_rows=args.band_rows, bg_paths=bg_paths,
                                 meters=meters, with_meters=is_under,
                                 annotate=is_under, merged_suffix=suffix)
            total += len(paths)
            print("%-24s -> %s" % (cam, ", ".join(Path(p).name for p in paths)))
    except (FrameSizeError, ProjectError) as exc:
        raise SystemExit(str(exc)) from None
    print("\n共写出 %d 个文件 -> %s" % (total, out_root))


def cmd_products(args):
    """按 PRODUCTS 配方跑四类标定产物，复用各子命令自己的 cmd_*，不复制逻辑。"""
    names = [args.only] if args.only else list(PRODUCTS)
    for name in names:
        steps = PRODUCTS.get(name)
        if steps is None:
            raise SystemExit("未知产物：%s（可选 %s）" % (name, ", ".join(PRODUCTS)))
        print("\n=== %s（%d 步）===" % (name, len(steps)))
        for step in steps:
            kind = step["kind"]
            dates = list(step.get("dates", ()))
            out_date = step.get("out_date") or (dates[0] if dates else args.date)
            out_root = object_frames_root(out_date)
            if args.dry_run:
                print("  %-10s %s -> %s" % (
                    kind, step.get("camera") or ",".join(step.get("cameras", ()))[:60],
                    out_root))
                continue
            # 复用真正的 cmd_*，参数用 Namespace 拼——配方只描述"跑什么"，
            # "怎么跑"仍只有一份实现。
            if kind == "merge":
                cmd_merge(argparse.Namespace(
                    project=None, date=dates[0], dates=dates or None,
                    root=str(DATASET), snapshots=None,
                    cameras=list(step["cameras"]), out_root=str(out_root),
                    band_rows=MO_BAND_ROWS, meter_spec=None,
                    meter_overrides=None, merged_suffix=None))
            elif kind == "auto_merge":
                cmd_auto_merge(argparse.Namespace(
                    camera=step["camera"], date=dates[0] if dates else args.date,
                    dates=dates or None, out_root=str(out_root),
                    thresh=step.get("thresh", DIST_THRESH),
                    band_rows=step.get("band_rows", BAND_ROWS),
                    merge_step=step.get("merge_step", 1)))
            elif kind == "grid":
                d = step.get("date", args.date)
                cmd_grid(argparse.Namespace(
                    date=d, input_dir=None, out=None, mesh_json=str(MESH_JSON)))
            else:
                raise SystemExit("配方里未知 kind：%s" % kind)


def cmd_grid(args):
    meters = camera_meters(args.mesh_json)
    in_dir = Path(args.input_dir) if args.input_dir else object_frames_root(args.date)
    out = Path(args.out) if args.out else in_dir / "underwater_mask_grid.png"
    path, info = stitch_4x4(in_dir, out, meters, date=args.date)
    for it in info:
        m = it["meters"]
        print("%-10s %s  %s  %s" % (it["cam"], it["state"],
                                    ("%.1fm" % m) if m is not None else "?m",
                                    ("%dx%d" % (it["shape"][1], it["shape"][0]))
                                    if it["shape"] else ""))
    print("\n拼出 %s" % path)


def cmd_label(args):
    from python.labeling.server import main as server_main
    sys.argv = ["server", "mask"] + \
        (["--port", str(args.port)] if args.port != 8765 else []) + \
        (["--no-browser"] if args.no_browser else [])
    server_main()


def main(argv=None):
    args = _parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
