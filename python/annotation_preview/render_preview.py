#!/usr/bin/env python3
"""Render 3 annotation-preview images for one representative snapshot.

Read-only against the dataset; writes only into outputs/annotation_preview/.
外部快照/分析数据集根通过 --dataset-root 或 ANNOTATION_PREVIEW_DATASET_ROOT
提供；输出统一落在 outputs/annotation_preview/（可用 --out-dir 覆盖）。
"""
import argparse
import csv
import os
from PIL import Image, ImageDraw, ImageFont

from python.annotation_preview import common as C

SNAP_DEFAULT = "raw_1783480173576_15"


def load_font(size):
    for p in [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def read_rows(csv_path, snap):
    if not os.path.exists(csv_path):
        raise SystemExit("缺少输入 CSV：%s（请通过 --dataset-root 或 "
                         "ANNOTATION_PREVIEW_DATASET_ROOT 指向有效数据集）" % csv_path)
    with open(csv_path) as f:
        return [r for r in csv.DictReader(f) if r["snapshot_id"] == snap]


def source_image(snap_dir, src):
    """Find the analysis-cam jpg for a source like 'underA5'."""
    for fn in os.listdir(snap_dir):
        if fn.endswith("__%s.jpg" % src):
            return os.path.join(snap_dir, fn)
    return None


def color_for(score):
    s = float(score)
    if s >= 5000:
        return (255, 60, 60)   # strong = red
    if s >= 500:
        return (255, 170, 0)   # medium = orange
    return (80, 200, 255)      # weak = cyan


# ---------- Sample A: single camera overlay (highest-score source) ----------
def render_single(ctx):
    bbox_rows, snap_dir, out, fonts, snap = (
        ctx["bbox_rows"], ctx["snap_dir"], ctx["out"], ctx["fonts"], ctx["snap"])
    f_med = fonts["med"]
    top = max(bbox_rows, key=lambda r: float(r["score"]))
    src = top["source"]
    img_path = source_image(snap_dir, src)
    im = Image.open(img_path).convert("RGB")
    scale = 2  # upscale so labels are readable
    im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
    d = ImageDraw.Draw(im)
    x, y, w, h = (int(top[k]) for k in ("x", "y", "w", "h"))
    x, y, w, h = x * scale, y * scale, w * scale, h * scale
    col = color_for(top["score"])
    d.rectangle([x, y, x + w, y + h], outline=col, width=3)
    cx, cy = float(top["cx"]) * scale, float(top["cy"]) * scale
    d.line([cx - 6, cy, cx + 6, cy], fill=col, width=2)
    d.line([cx, cy - 6, cx, cy + 6], fill=col, width=2)
    label = "%s  score=%s  %sx%s" % (src, top["score"], top["w"], top["h"])
    d.rectangle([x, y - 22, x + 8 + len(label) * 8, y], fill=col)
    d.text((x + 4, y - 20), label, fill=(0, 0, 0), font=f_med)
    d.text((6, 6), "Sample A - single camera overlay | snapshot %s" % snap,
           fill=(255, 255, 0), font=f_med)
    dst = os.path.join(out, "sample-A-single-camera.jpg")
    im.save(dst, quality=90)
    print("wrote", dst, "src", src)


# ---------- Sample B: contact grid of all underwater cams with boxes ----------
def render_grid(ctx):
    bbox_rows, snap_dir, out, fonts, snap = (
        ctx["bbox_rows"], ctx["snap_dir"], ctx["out"], ctx["fonts"], ctx["snap"])
    f_big, f_med = fonts["big"], fonts["med"]
    cams = ["underA%d" % i for i in range(1, 17)]
    box_by_src = {r["source"]: r for r in bbox_rows}
    cell_w, cell_h, pad, header = 640, 360, 6, 26
    cols = 4
    rows = (len(cams) + cols - 1) // cols
    W = cols * (cell_w + pad) + pad
    H = rows * (cell_h + header + pad) + pad + 30
    canvas = Image.new("RGB", (W, H), (20, 20, 24))
    d = ImageDraw.Draw(canvas)
    d.text((pad, 6), "Sample B - all underwater cameras + detections | snapshot %s"
           % snap, fill=(255, 255, 0), font=f_big)
    for idx, cam in enumerate(cams):
        r, c = divmod(idx, cols)
        ox = pad + c * (cell_w + pad)
        oy = 30 + pad + r * (cell_h + header + pad)
        img_path = source_image(snap_dir, cam)
        has = cam in box_by_src
        title_col = color_for(box_by_src[cam]["score"]) if has else (120, 120, 120)
        d.rectangle([ox, oy, ox + cell_w, oy + header], fill=title_col)
        cap = cam + (" score=%s" % box_by_src[cam]["score"] if has else " (none)")
        d.text((ox + 4, oy + 4), cap, fill=(0, 0, 0), font=f_med)
        if img_path:
            sub = Image.open(img_path).convert("RGB").resize((cell_w, cell_h))
            canvas.paste(sub, (ox, oy + header))
            if has:
                b = box_by_src[cam]
                sx, sy = cell_w / 640.0, cell_h / 360.0
                x, y = int(b["x"]) * sx, int(b["y"]) * sy
                w, h = int(b["w"]) * sx, int(b["h"]) * sy
                dd = ImageDraw.Draw(canvas)
                dd.rectangle([ox + x, oy + header + y, ox + x + w,
                              oy + header + y + h], outline=title_col, width=3)
    dst = os.path.join(out, "sample-B-contact-grid.jpg")
    canvas.save(dst, quality=88)
    print("wrote", dst)


# ---------- Sample C: panorama projection ----------
def render_pano(ctx):
    pano_rows, out, fonts, snap = (
        ctx["pano_rows"], ctx["out"], ctx["fonts"], ctx["snap"])
    f_big, f_small = fonts["big"], fonts["small"]
    xs = [float(r["pano_x_approx"]) for r in pano_rows]
    ys = [float(r["pano_y_approx"]) for r in pano_rows]
    pano_w = int(max(float(r["pano_end"]) for r in pano_rows) + 100)
    W, H, top = pano_w, 700, 60
    canvas = Image.new("RGB", (W, H), (12, 30, 55))
    d = ImageDraw.Draw(canvas)
    d.text((10, 10), "Sample C - panorama projection (pano_x/pano_y) | snapshot %s"
           % snap, fill=(255, 255, 0), font=f_big)
    # x-axis ticks every 500 px of pano coord
    for gx in range(0, pano_w, 500):
        d.line([gx, top, gx, H - 20], fill=(40, 60, 90), width=1)
        d.text((gx + 3, H - 18), "pano_x=%d" % gx, fill=(120, 160, 200), font=f_small)
    y_scale = (H - top - 40) / 360.0
    for r in sorted(pano_rows, key=lambda r: float(r["score"])):
        px, py = float(r["pano_x_approx"]), float(r["pano_y_approx"])
        w, h = int(r["w"]), int(r["h"])
        col = color_for(r["score"])
        cy = top + py * y_scale
        x0, x1 = px - w / 2, px + w / 2
        y0, y1 = cy - h / 2 * y_scale, cy + h / 2 * y_scale
        d.rectangle([x0, y0, x1, y1], outline=col, width=3)
        lab = "%s  s=%s" % (r["source"], r["score"])
        d.text((x0, y0 - 16), lab, fill=col, font=f_small)
    dst = os.path.join(out, "sample-C-pano.jpg")
    canvas.save(dst, quality=88)
    print("wrote", dst, "pano_w", pano_w)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset-root", default=C.DATASET,
                    help="外部快照/分析数据集根（默认取 ANNOTATION_PREVIEW_DATASET_ROOT，"
                         "当前 %(default)s）")
    ap.add_argument("--out-dir", default=C.OUTPUT_ROOT,
                    help="预览图输出目录（默认 %(default)s）")
    ap.add_argument("--snapshot", default=SNAP_DEFAULT,
                    help="代表快照 id（默认 %(default)s）")
    args = ap.parse_args()

    snap = args.snapshot
    dataset_root = args.dataset_root
    out = args.out_dir
    snap_dir = os.path.join(dataset_root, "snapshots", snap)
    bbox_csv = os.path.join(dataset_root, "analysis", "underwater-motion-bboxes.csv")
    pano_csv = os.path.join(
        dataset_root, "analysis", "underwater-motion-bboxes-with-pano.csv")

    bbox_rows = read_rows(bbox_csv, snap)
    pano_rows = read_rows(pano_csv, snap)
    if not os.path.isdir(snap_dir):
        raise SystemExit("缺少快照目录：%s" % snap_dir)
    print("bbox rows:", len(bbox_rows), "pano rows:", len(pano_rows))

    ctx = {
        "bbox_rows": bbox_rows,
        "pano_rows": pano_rows,
        "snap_dir": snap_dir,
        "out": out,
        "snap": snap,
        "fonts": {"big": load_font(18), "med": load_font(14), "small": load_font(12)},
    }
    os.makedirs(out, exist_ok=True)
    render_single(ctx)
    render_grid(ctx)
    render_pano(ctx)
    print("done")


if __name__ == "__main__":
    main()
