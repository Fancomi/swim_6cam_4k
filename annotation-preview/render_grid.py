#!/usr/bin/env python3
"""逐相机标注网格图（严格保持底图原始像素尺寸，零缩放/零偏移，便于逐像素对齐 live）。

每个 underAx 单独输出一张：底图取该相机「点列最居中」的代表帧，将该相机
所有已标注帧的 9 点列按原始图像坐标直接叠加，连成网格——
  竖线：同一帧的 9 个点（列，按全局帧号标注 F<NN>）
  横线：相邻帧的同序号点（行，标注 R1..R9）
所有文字/图元均画在图内，不改变画布尺寸。产物落在 object-frames/annotation-grids/。
"""
import os
from PIL import Image, ImageDraw
import common as C

OUT_DIR = os.path.join(C.OBJ_DIR, "annotation-grids")
COL = {"dot": (255, 212, 121), "mesh": (90, 200, 255),
       "rep": (120, 240, 180), "txt": (230, 238, 246)}


def labeled_columns(frames):
    """取该相机所有 9 点帧，返回 [(frame_index, 按 y 排序的点列)]。"""
    return [(fi, sorted(pts, key=lambda q: q["y"]))
            for fi, _rel, pts in frames if len(pts) == C.N_ROWS]


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
    canvas = Image.open(C.find_image(rel_of[rep[0]])).convert("RGB")
    d = ImageDraw.Draw(canvas)

    def px(pt):
        return (pt["x"], pt["y"])

    for r in range(C.N_ROWS):                              # 横线（行）
        d.line([px(col[r]) for _fi, col in cols], fill=COL["mesh"], width=1)
    for fi, col in cols:                                   # 竖线（列）+ 帧号
        d.line([px(p) for p in col], fill=COL["mesh"], width=1)
        tx, ty = px(col[0])
        d.text((tx - 8, max(1, ty - 14)), "F%02d" % fi, fill=COL["txt"], font=font_lbl)
    for fi, col in cols:                                   # 点（代表帧高亮）
        c = COL["rep"] if fi == rep[0] else COL["dot"]
        for p in col:
            x, y = px(p)
            d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=c, outline=(20, 20, 20))
    for r, p in enumerate(rep[1]):                         # 行号（贴代表帧列左侧）
        _x, y = px(p)
        d.text((4, max(1, y - 7)), "R%d" % (r + 1), fill=COL["rep"], font=font_lbl)
    d.text((6, 6), "%s | rep F%02d | %d frames" % (cam, rep[0], len(cols)),
           fill=COL["txt"], font=font_hdr)

    canvas.save(os.path.join(OUT_DIR, "%s-grid.png" % cam))
    return rep[0], len(cols), canvas.size


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    groups = C.group_by_camera(C.load_project())
    font_hdr, font_lbl = C.load_font(15), C.load_font(12)
    print("rendering per-camera grids -> %s" % OUT_DIR)
    for cam in C.CAMS_PANO:
        res = render_camera(cam, groups.get(cam, []), font_hdr, font_lbl)
        if res:
            print("  %-8s rep F%02d  cols=%d  size=%dx%d"
                  % (cam, res[0], res[1], res[2][0], res[2][1]))
        else:
            print("  %-8s (no labeled frames)" % cam)


if __name__ == "__main__":
    main()
