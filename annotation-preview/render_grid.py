#!/usr/bin/env python3
"""逐相机标注网格图（严格保持底图原始像素尺寸，零缩放/零偏移，便于逐像素对齐 live）。

每个 underAx 单独输出一张：底图取该相机「点列最居中」的代表帧，将该相机
所有已标注帧的 9 点列按原始图像坐标直接叠加，连成网格——
  竖线：同一帧的 9 个点（列，按真实纵向距离标注，单位米：0.5×帧号）
  横线：相邻帧的同序号点（行，按真实横向距离标注，单位米：自底向上，最下行为 0m）
若某图内部存在缺失列（相邻已标注帧之间跳号），按帧号比例线性插值补出该列，
并以区别色标出。所有文字/图元均画在图内，不改变画布尺寸。
产物落在数据集根的 annotation-grids/（与 object-frames/ 同级）。
"""
import os
from PIL import Image, ImageDraw
import common as C

OUT_DIR = os.path.join(C.DATASET, "annotation-grids")
COL = {"dot": (255, 212, 121), "mesh": (90, 200, 255),
       "rep": (120, 240, 180), "txt": (255, 255, 255),
       "interp": (255, 170, 60), "stroke": (0, 0, 0)}

COL_METRES = 0.5      # 每个帧号（列）对应的纵向米数：F01=0.5m, F02=1.0m ...
ROW_METRES = 0.25     # 相邻行的横向米数；自底向上计，最下行为 0.00m


def col_label(fi):
    """帧号 -> 纵向距离标签（米）。"""
    return "%.1fm" % (COL_METRES * fi)


def row_label(r, n_rows):
    """行序（0=最上）-> 横向距离标签（米），自底向上，最下行为 0.00m。"""
    return "%.2fm" % (ROW_METRES * (n_rows - 1 - r))


def text_hc(d, xy, text, font, fill):
    """高对比文字：深色描边 + 亮色填充，适配蓝色水下背景。"""
    d.text(xy, text, font=font, fill=fill,
           stroke_width=2, stroke_fill=COL["stroke"])


def labeled_columns(frames):
    """取该相机所有 9 点帧，返回 [(frame_index, 按 y 排序的点列)]。"""
    return [(fi, sorted(pts, key=lambda q: q["y"]))
            for fi, _rel, pts in frames if len(pts) == C.N_ROWS]


def fill_column_gaps(cols):
    """按帧号线性插值补齐内部缺失列。

    输入 [(fi, 9点列)]；返回 [(fi, 9点列, is_interp)]，其中相邻已标注帧之间
    跳过的整数帧号被逐点线性插值补出（is_interp=True）。"""
    cols = sorted(cols, key=lambda c: c[0])
    by_fi = {fi: col for fi, col in cols}
    fis = [fi for fi, _ in cols]
    out = []
    for a, b in zip(fis, fis[1:]):
        out.append((a, by_fi[a], False))
        for mid in range(a + 1, b):
            t = (mid - a) / (b - a)
            col = [{"x": pa["x"] + (pb["x"] - pa["x"]) * t,
                    "y": pa["y"] + (pb["y"] - pa["y"]) * t}
                   for pa, pb in zip(by_fi[a], by_fi[b])]
            out.append((mid, col, True))
    out.append((fis[-1], by_fi[fis[-1]], False))
    return out


def pick_rep(cols, cx):
    """代表帧 = 点列均值 x 最接近画面水平中心者。"""
    return min(cols, key=lambda fc: abs(sum(p["x"] for p in fc[1]) / C.N_ROWS - cx))


def render_camera(cam, frames, font_hdr, font_lbl):
    cols = labeled_columns(frames)
    if not cols:
        return None
    rel_of = {fi: rel for fi, rel, _ in frames}
    W = Image.open(C.find_image(rel_of[cols[0][0]])).size[0]   # 原始画布宽（仅用于取中心）
    rep = pick_rep(cols, W / 2)
    filled = fill_column_gaps(cols)
    n_interp = sum(1 for _fi, _col, itp in filled if itp)
    canvas = Image.open(C.find_image(rel_of[rep[0]])).convert("RGB")
    d = ImageDraw.Draw(canvas)

    def px(pt):
        return (pt["x"], pt["y"])

    for r in range(C.N_ROWS):                              # 横线（行）
        d.line([px(col[r]) for _fi, col, _itp in filled], fill=COL["mesh"], width=1)
    for fi, col, itp in filled:                            # 竖线（列）+ 纵向距离
        d.line([px(p) for p in col], fill=COL["mesh"], width=1)
        tx, ty = px(col[0])
        lbl_col = COL["interp"] if itp else COL["txt"]
        text_hc(d, (tx - 8, max(1, ty - 16)), col_label(fi), font_lbl, lbl_col)
    for fi, col, itp in filled:                            # 点（代表帧/插值列高亮）
        if itp:
            c = COL["interp"]
        elif fi == rep[0]:
            c = COL["rep"]
        else:
            c = COL["dot"]
        for p in col:
            x, y = px(p)
            d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=c, outline=(20, 20, 20))
    for r, p in enumerate(rep[1]):                         # 行序距离（贴代表帧列左侧）
        _x, y = px(p)
        text_hc(d, (4, max(1, y - 7)), row_label(r, C.N_ROWS), font_lbl, COL["rep"])
    text_hc(d, (6, 6), "%s | rep %s | %d cols (+%d interp)"
            % (cam, col_label(rep[0]), len(cols), n_interp), font_hdr, COL["txt"])

    canvas.save(os.path.join(OUT_DIR, "%s-grid.png" % cam))
    return rep[0], len(cols), n_interp, canvas.size


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    groups = C.group_by_camera(C.load_project())
    font_hdr, font_lbl = C.load_font(15), C.load_font(12)
    print("rendering per-camera grids -> %s" % OUT_DIR)
    for cam in C.CAMS_PANO:
        res = render_camera(cam, groups.get(cam, []), font_hdr, font_lbl)
        if res:
            print("  %-8s rep %s  cols=%d  interp=%d  size=%dx%d"
                  % (cam, col_label(res[0]), res[1], res[2], res[3][0], res[3][1]))
        else:
            print("  %-8s (no labeled frames)" % cam)


if __name__ == "__main__":
    main()
