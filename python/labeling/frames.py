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
              中值背景）。处理工程里存在的所有相机。
  grid        仅 underwater：把 16 相机的 mask 合成图按 4×4 cat 拼成一张大图，
              每格标注相机 ID + 泳道米数（FBX 世界 X），并标注每帧 mask 的帧 ID
              与米数。
  label       起浏览器 mask 标注器（转发 server.py）。

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


def median_background_streaming(paths, band_rows=BAND_ROWS):
    """逐像素中值背景，内存按条带封顶；与 merge_overhead.median_background 逐位一致。"""
    first = _decode(paths[0])
    height, width = first.shape[:2]
    out = np.empty((height, width, 3), dtype=np.uint8)
    for y0, y1 in _bands(height, band_rows):
        stack = np.stack([_decode_band(p, y0, y1) for p in paths])
        out[y0:y1] = np.median(stack, axis=0).astype(np.uint8)
    return out


def merge_frames_streaming(paths, background, thresh=DIST_THRESH,
                           band_rows=BAND_ROWS):
    """把每帧前景按时间序叠到背景上（后帧覆盖前帧），流式逐帧、峰值内存一帧。"""
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


def _resolve_images(project_cam, snapshots_root):
    """工程 image 相对路径 → (snapshot_id, 绝对路径, 该帧笔画)。按文件名反查快照目录。"""
    resolved = []
    snap_root = Path(snapshots_root)
    for f in project_cam:
        image = f.get("image")
        if not image:
            continue
        rel, fname = Path(image), Path(image).name
        hits = []
        cand = snap_root / rel.parent if rel.parent.parts else snap_root
        if cand.is_dir():
            hits = sorted(cand.glob("*" + fname))
        if not hits:
            hits = sorted(snap_root.glob("raw_*/*" + fname))
        if not hits:
            raise ProjectError("工程引用的帧找不到：%s（在 %s 下）" % (image, snap_root))
        resolved.append((hits[0].parent.name, str(hits[0]), f.get("strokes") or []))
    return resolved


def merge_camera(camera, project_cam, snapshots_root, out_dir,
                 band_rows=MO_BAND_ROWS):
    """手动合成一台相机：读帧栈 -> 中值背景 -> 逐帧按该帧 mask 叠前景，写两张 PNG。

    逐帧用该帧自己的笔画（mask_labeler 保存的 strokes），帧间位置不同不会互相污染。
    """
    entries = _resolve_images(project_cam, snapshots_root)
    if not entries:
        raise ProjectError("该相机工程里没有可解析的帧")
    stack = load_stack([p for _sid, p, _st in entries])
    h, w = stack.shape[1], stack.shape[2]
    background = median_background(stack, band_rows=band_rows)
    merged = background.copy()
    for frame, (_sid, _p, strokes) in zip(stack, entries):
        if not strokes:
            continue
        mask = rasterize_strokes(strokes, w, h)
        merged[mask] = frame[mask]
    out_dir = Path(out_dir)
    paths = []
    for suffix, image in (("mask_background", background), ("mask_merged", merged)):
        path = write_image(out_dir / ("%s_%s.png" % (camera, suffix)),
                           image[:, :, ::-1], "mask-merge")
        paths.append(str(path))
    return paths


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
            write_image(out_root / ("%s_median_background.png" % cam),
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


def _uv_to_world_x(mesh):
    """由网格三角形拟合 UV → 世界 X 的线性映射，返回函数 u,v -> x。

    underwater 平面是矩形、const_axis=1（Y 恒定），世界 X 由 UV 唯一决定，
    用最小二乘拟合 x = a*u + b*v + c。返回 (a, b, c) 系数。
    """
    pts = []
    for tri in mesh.get("triangles", []):
        for v in tri:
            if "uv" in v and "pos" in v:
                pts.append((v["uv"][0], v["uv"][1], v["pos"][0]))
    if len(pts) < 3:
        return None
    A = np.array([[u, v, 1.0] for u, v, _x in pts], dtype=np.float64)
    b = np.array([x for _u, _v, x in pts], dtype=np.float64)
    coef, *_ = np.linalg.lstsq(A, b, rcond=None)
    return coef


def stitch_4x4(input_dir, out_path, meters, project=None, date=DEFAULT_DATE,
               cameras=UNDER_CAMERAS):
    """把 16 相机 mask 合成图按 4×4 cat 拼接，每格标注相机 ID + 米数。

    project 给定时，每格额外标注每帧 mask 的帧 ID 与米数（读工程笔画换算）。
    缺图格子留淡底 + 仍标相机 ID/米数。返回 (输出路径, 每格信息)。
    """
    input_dir = Path(input_dir)
    meshes_by_cam = {}
    if project is not None and Path(MESH_JSON).is_file():
        doc = json.loads(Path(MESH_JSON).read_text(encoding="utf-8"))
        for mesh in doc.get("meshes", []):
            tex = mesh.get("texture_basename", "")
            cam = next((c for c, t in TEX_NAME.items() if t == tex), None)
            if cam is not None:
                meshes_by_cam["underA%d" % cam] = mesh

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
            # 每帧 mask 标注：帧 ID + 米数，画在该帧笔画中心附近
            if project is not None and cam in meshes_by_cam:
                _draw_frame_labels(canvas, project.get("cameras", {}).get(cam, []),
                                   meshes_by_cam[cam], y1, x1, h, w)
        m = meters.get(cam)
        label = cam + ("  %.1fm" % m if m is not None else "")
        _put_label(canvas, label, x0 + 2, y0 + (label_h - 15) // 2, bg=(28, 32, 40))

    write_image(out_path, canvas, "mask-grid")
    return str(out_path), info


def _draw_frame_labels(canvas, project_cam, mesh, y0, x0, h, w):
    """在合成图上把工程每帧的 mask 笔画中心画上「帧ID 米数」标签。"""
    coef = _uv_to_world_x(mesh)
    if coef is None:
        return
    a, b, c = coef
    for f in project_cam:
        strokes = f.get("strokes") or []
        if not strokes:
            continue
        xs = [float(s["x1"]) + float(s["x2"]) for s in strokes]
        ys = [float(s["y1"]) + float(s["y2"]) for s in strokes]
        cx = sum(xs) / (2 * len(xs))
        cy = sum(ys) / (2 * len(ys))
        # 图像像素 x → UV u（整幅平铺）→ 世界 X → 泳道米数
        u = cx / max(1, w - 1)
        world_x = a * u + b * 0.5 + c        # v 取中间行（v 不参与 X 变化的平面）
        # 平移：米数 = world_x - lane_min（需 lane_min，从 mesh 的 pos 推）
        all_x = [v["pos"][0] for tri in mesh.get("triangles", []) for v in tri]
        meters = world_x - min(all_x)
        px, py = int(x0 + cx), int(y0 + cy)
        _put_label(canvas, "f%02d %.1fm" % (f.get("frame_index", 0), meters),
                   px, py - 8)


def _put_label(canvas, text, x, y, bg=(10, 10, 10)):
    """在 (x,y) 画白字黑底条（bg 可换淡底）。"""
    font = cv2.FONT_HERSHEY_SIMPLEX
    scale, thick = 0.5, 1
    (tw, th), _ = cv2.getTextSize(text, font, scale, thick)
    pad = 3
    cv2.rectangle(canvas, (x, y), (x + tw + 2 * pad, y + th + 2 * pad), bg,
                  thickness=-1)
    cv2.putText(canvas, text, (x + pad, y + th + pad), font, scale,
                (255, 255, 255), thick, cv2.LINE_AA)


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
                   help="额外写 <相机>_median_background.png 诊断图")
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
    p.add_argument("--root", default=str(DATASET))
    p.add_argument("--snapshots", default=None)
    p.add_argument("--cameras", nargs="+", default=None,
                   help="相机子集（默认工程里有的全部）")
    p.add_argument("--out-root", default=None)
    p.add_argument("--band-rows", type=int, default=MO_BAND_ROWS)
    p.set_defaults(func=cmd_merge)

    p = sub.add_parser("grid", help="仅水下：16 相机 4×4 拼接")
    p.add_argument("--date", default=DEFAULT_DATE)
    p.add_argument("--input-dir", default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--project", default=None, help="mask 工程，标注每帧帧 ID/米数")
    p.add_argument("--mesh-json", default=str(MESH_JSON))
    p.set_defaults(func=cmd_grid)

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


def cmd_merge(args):
    snap_root = Path(args.snapshots) if args.snapshots else \
        snapshots_dir(args.date)
    if not snap_root.is_dir():
        raise SystemExit("缺少快照目录：%s" % snap_root)
    project = Path(args.project) if args.project else \
        snap_root.parent / PROJECT_FILENAME
    if not project.is_file():
        raise SystemExit("缺少工程文件：%s（请先跑 label 画保留区域）" % project)
    cameras = load_project(project)
    out_root = Path(args.out_root) if args.out_root else object_frames_root(args.date)
    cams = tuple(args.cameras) if args.cameras else \
        [c for c in cameras if cameras.get(c)]
    total = 0
    try:
        for cam in cams:
            project_cam = cameras.get(cam)
            if not project_cam:
                print("%-24s 工程里没有该相机，跳过" % cam)
                continue
            paths = merge_camera(cam, project_cam, snap_root,
                                 out_root, band_rows=args.band_rows)
            total += len(paths)
            print("%-24s -> %s" % (cam, ", ".join(paths)))
    except (FrameSizeError, ProjectError) as exc:
        raise SystemExit(str(exc)) from None
    print("\n共写出 %d 个文件 -> %s" % (total, out_root))


def cmd_grid(args):
    meters = camera_meters(args.mesh_json)
    in_dir = Path(args.input_dir) if args.input_dir else object_frames_root(args.date)
    out = Path(args.out) if args.out else in_dir / "underwater_mask_grid.png"
    project = None
    if args.project:
        project = load_project(args.project)
    path, info = stitch_4x4(in_dir, out, meters, project=project, date=args.date)
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
