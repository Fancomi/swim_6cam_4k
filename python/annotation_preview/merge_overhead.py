#!/usr/bin/env python3
"""把一台相机全时段的快照合成为一张 UV 参考图。

50 帧逐像素中值作背景帧；每帧与背景的 RGB 欧氏距离超阈判为前景；
按时间顺序把前景叠到背景上，后帧覆盖前帧。中值与差分按水平条带计算，
避免 4K 尺寸下 float32 中间量吃满内存。
"""
import colorsys
import datetime
import re

import numpy as np
from PIL import Image, ImageDraw

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
