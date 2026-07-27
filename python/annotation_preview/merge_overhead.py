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
