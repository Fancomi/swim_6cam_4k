#!/usr/bin/env python3
"""Render 3 annotation-preview images for one representative snapshot.

Read-only against the dataset; writes only into annotation-preview/.
"""
import csv
import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "annotation-preview")
SNAP = "raw_1783480173576_15"
SNAP_DIR = os.path.join(ROOT, "snapshots", SNAP)
BBOX_CSV = os.path.join(ROOT, "analysis", "underwater-motion-bboxes.csv")
PANO_CSV = os.path.join(ROOT, "analysis", "underwater-motion-bboxes-with-pano.csv")


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
    with open(csv_path) as f:
        return [r for r in csv.DictReader(f) if r["snapshot_id"] == snap]


def source_image(src):
    """Find the analysis-cam jpg for a source like 'underA5'."""
    for fn in os.listdir(SNAP_DIR):
        if fn.endswith("__%s.jpg" % src):
            return os.path.join(SNAP_DIR, fn)
    return None


def color_for(score):
    s = float(score)
    if s >= 5000:
        return (255, 60, 60)   # strong = red
    if s >= 500:
        return (255, 170, 0)   # medium = orange
    return (80, 200, 255)      # weak = cyan


bbox_rows = read_rows(BBOX_CSV, SNAP)
pano_rows = read_rows(PANO_CSV, SNAP)
f_big = load_font(18)
f_med = load_font(14)
f_small = load_font(12)
print("bbox rows:", len(bbox_rows), "pano rows:", len(pano_rows))


# ---------- Sample A: single camera overlay (highest-score source) ----------
def render_single():
    top = max(bbox_rows, key=lambda r: float(r["score"]))
    src = top["source"]
    img_path = source_image(src)
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
    d.text((6, 6), "Sample A - single camera overlay | snapshot %s" % SNAP,
           fill=(255, 255, 0), font=f_med)
    out = os.path.join(OUT, "sample-A-single-camera.jpg")
    im.save(out, quality=90)
    print("wrote", out, "src", src)


# ---------- Sample B: contact grid of all underwater cams with boxes ----------
def render_grid():
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
           % SNAP, fill=(255, 255, 0), font=f_big)
    for idx, cam in enumerate(cams):
        r, c = divmod(idx, cols)
        ox = pad + c * (cell_w + pad)
        oy = 30 + pad + r * (cell_h + header + pad)
        img_path = source_image(cam)
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
    out = os.path.join(OUT, "sample-B-contact-grid.jpg")
    canvas.save(out, quality=88)
    print("wrote", out)


# ---------- Sample C: panorama projection ----------
def render_pano():
    xs = [float(r["pano_x_approx"]) for r in pano_rows]
    ys = [float(r["pano_y_approx"]) for r in pano_rows]
    pano_w = int(max(float(r["pano_end"]) for r in pano_rows) + 100)
    W, H, top = pano_w, 700, 60
    canvas = Image.new("RGB", (W, H), (12, 30, 55))
    d = ImageDraw.Draw(canvas)
    d.text((10, 10), "Sample C - panorama projection (pano_x/pano_y) | snapshot %s"
           % SNAP, fill=(255, 255, 0), font=f_big)
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
    out = os.path.join(OUT, "sample-C-pano.jpg")
    canvas.save(out, quality=88)
    print("wrote", out, "pano_w", pano_w)


render_single()
render_grid()
render_pano()
print("done")
