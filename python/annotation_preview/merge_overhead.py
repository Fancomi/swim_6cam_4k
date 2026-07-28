#!/usr/bin/env python3
"""把一台相机全时段的快照合成为一张 UV 参考图。

50 帧逐像素中值作背景帧；每帧与背景的 RGB 欧氏距离超阈判为前景；
按时间顺序把前景叠到背景上，后帧覆盖前帧。中值与差分按水平条带计算，
避免 4K 尺寸下 float32 中间量吃满内存。输出始终是相机原始分辨率。
"""
import argparse
import os

import numpy as np
from PIL import Image

from python.annotation_preview import common as C

# 与水下相机无关的三台：两台高空俯视 + 一台 orbbec，快照时刻完全相同。
CAMERAS = ("overhead5", "overhead6", "orbbec_camera_1")
BAND_ROWS = 256                                     # 分带高度，压住 float32 峰值内存


def bands(height, band_rows):
    """把 [0, height) 切成 [(y0, y1)] 条带。"""
    step = max(1, int(band_rows))
    return [(y, min(y + step, height)) for y in range(0, height, step)]


def median_background(stack, band_rows=BAND_ROWS):
    """逐像素取时间轴中值，得到干净空池背景帧。"""
    _n, h, w, _c = stack.shape
    out = np.empty((h, w, 3), dtype=np.uint8)
    for y0, y1 in bands(h, band_rows):
        out[y0:y1] = np.median(stack[:, y0:y1].astype(np.float32), axis=0).astype(np.uint8)
    return out


def merge_frames(stack, background, thresh=C.DIST_THRESH, band_rows=BAND_ROWS):
    """按时间顺序把每帧前景叠到背景上（后帧覆盖前帧），返回合成图。"""
    n, h, _w, _c = stack.shape
    merged = background.copy()
    base = background.astype(np.float32)
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
    return merged


OUT_DIR = os.path.join(C.OUTPUT_ROOT, "overhead-merge")


class FrameSizeError(Exception):
    """同一相机各帧尺寸不一致；静默缩放会产出错误的参考图，所以直接失败。"""


def load_stack(paths):
    """解码一组同尺寸图像进 (N, H, W, 3) uint8 栈，保持原始分辨率。"""
    frames, size = [], None
    for path in paths:
        im = Image.open(path).convert("RGB")
        if size is None:
            size = im.size
        elif im.size != size:
            raise FrameSizeError(
                "帧尺寸不一致：%s 为 %dx%d，期望 %dx%d"
                % (os.path.basename(path), im.size[0], im.size[1], size[0], size[1]))
        frames.append(np.asarray(im, dtype=np.uint8))
    return np.stack(frames, axis=0)


def run_camera(cam, out_dir=OUT_DIR, thresh=C.DIST_THRESH, band_rows=BAND_ROWS):
    """跑完一台相机：中值背景 -> 前景叠加，写出两张原始分辨率 PNG。"""
    frames = C.frames_for_camera(cam)
    if not frames:
        print("%-16s 无匹配帧，跳过" % cam)
        return []

    stack = load_stack([p for _sid, p in frames])
    print("%-16s frames=%d  %dx%d" % (cam, len(stack), stack.shape[2], stack.shape[1]))
    background = median_background(stack, band_rows=band_rows)
    merged = merge_frames(stack, background, thresh=thresh, band_rows=band_rows)

    os.makedirs(out_dir, exist_ok=True)
    written = []
    for suffix, image in (("background", background), ("merged", merged)):
        path = os.path.join(out_dir, "%s_%s.png" % (cam, suffix))
        Image.fromarray(image).save(path)
        written.append(path)
        print("  wrote %s" % path)
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cameras", nargs="+", default=list(CAMERAS),
                    help="要处理的相机（默认 %(default)s）")
    ap.add_argument("--thresh", type=float, default=C.DIST_THRESH,
                    help="前景判定的 RGB 距离阈值（默认 %(default)s）")
    ap.add_argument("--band-rows", type=int, default=BAND_ROWS,
                    help="分带高度，越小越省内存（默认 %(default)s）")
    ap.add_argument("--out-dir", default=OUT_DIR,
                    help="输出目录（默认 %(default)s）")
    args = ap.parse_args(argv)

    if not os.path.isdir(C.SNAP_DIR):
        raise SystemExit(
            "缺少快照目录：%s（请通过 ANNOTATION_PREVIEW_DATASET_ROOT 指向有效数据集）"
            % C.SNAP_DIR)

    total = 0
    try:
        for cam in args.cameras:
            total += len(run_camera(cam, out_dir=args.out_dir, thresh=args.thresh,
                                    band_rows=args.band_rows))
    except FrameSizeError as exc:
        raise SystemExit(str(exc)) from None
    print("\n共写出 %d 个文件 -> %s" % (total, args.out_dir))


if __name__ == "__main__":
    main()
