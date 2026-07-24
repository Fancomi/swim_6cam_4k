"""Render the underwater stitch from a mesh JSON: still + grid diagnostic.

Reuses python.validation.reference_renderer for all remap/feather/composite/grid
logic; this module only adds underwater defaults, width-adaptive ppm, and an
isolated CLI that writes into outputs/underwater/.
"""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from python.validation import reference_renderer as rr

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"


def resolve_ppm(xmin, xmax, target_width):
    """Pixels-per-metre so world-X span maps to ~target_width px. Fallback 100.0."""
    span = xmax - xmin
    if span <= 0:
        return 100.0
    return target_width / span


def build_remap_clipped(mesh, tex_w, tex_h, xmin, ymin, ppm, out_w, out_h):
    """Like reference_renderer.build_remap, but coverage excludes samples whose
    UV falls outside the real image [0,1] range.

    The reused build_remap marks every rasterised pixel as covered and lets
    cv2.remap sample out-of-bounds UVs via border reflection — which paints a
    mirrored strip of the neighbour's edge exactly at each block's border and
    shows up as a hard mis-registered column at the seam. Here a pixel is
    covered only if its source coordinate lands inside the texture, so blocks
    stop at their real image extent and the neighbour takes over cleanly."""
    mapx = np.zeros((out_h, out_w), np.float32)
    mapy = np.zeros((out_h, out_w), np.float32)
    mask = np.zeros((out_h, out_w), np.uint8)
    for tri in mesh["triangles"]:
        dst = np.array([[(v["pos"][0] - xmin) * ppm,
                         out_h - 1 - (v["pos"][1] - ymin) * ppm] for v in tri], np.float32)
        src = np.array([[v["uv"][0] * tex_w,
                         (1.0 - v["uv"][1]) * tex_h] for v in tri], np.float32)
        x, y, w, h = cv2.boundingRect(dst)
        if w <= 0 or h <= 0:
            continue
        dloc = (dst - np.float32([x, y])).astype(np.float32)
        M = cv2.getAffineTransform(dloc, src)
        gy, gx = np.mgrid[0:h, 0:w]
        gx = gx.astype(np.float32)
        gy = gy.astype(np.float32)
        sx = M[0, 0] * gx + M[0, 1] * gy + M[0, 2]
        sy = M[1, 0] * gx + M[1, 1] * gy + M[1, 2]
        mloc = np.zeros((h, w), np.uint8)
        cv2.fillConvexPoly(mloc, np.int32(dloc), 1)
        inside = (mloc > 0) & (sx >= 0) & (sx <= tex_w - 1) & (sy >= 0) & (sy <= tex_h - 1)
        mapx[y:y + h, x:x + w][inside] = sx[inside]
        mapy[y:y + h, x:x + w][inside] = sy[inside]
        mask[y:y + h, x:x + w][inside] = 1
    m1, m2 = cv2.convertMaps(mapx, mapy, cv2.CV_16SC2)
    return m1, m2, mask.astype(np.uint8)


def crop_bottom_and_scale(image, crop_px, target_height, interpolation=cv2.INTER_LINEAR):
    """Crop `crop_px` rows from the bottom, then scale proportionally to target height."""
    if crop_px < 0 or crop_px >= image.shape[0]:
        raise ValueError(f"crop_px must be in [0, {image.shape[0] - 1}]")
    cropped = image[:-crop_px] if crop_px else image
    if cropped.shape[0] == target_height:
        return cropped
    scale = target_height / cropped.shape[0]
    target_width = int(round(cropped.shape[1] * scale))
    return cv2.resize(cropped, (target_width, target_height), interpolation=interpolation)


def bottom_dirty_rows(coverage):
    """Count contiguous rows at the BOTTOM that contain an uncovered (black) pixel.

    `coverage` is the union coverage mask (>0 where some layer painted the
    pixel). The perspective floor of the shorter planes leaves ragged black
    gaps along the bottom of the stitched canvas; a row is 'clean' only when it
    is covered as widely as the fullest row (the constant `margin` padding means
    even a fully-covered row keeps a few zero columns, so the reference is the
    per-canvas maximum, not the full width). Returns the number of bottom rows
    to drop so the result starts on the first fully-covered row from below."""
    cov = (coverage > 0).sum(axis=1)
    full = int(cov.max())
    crop = 0
    for r in range(coverage.shape[0] - 1, -1, -1):
        if cov[r] >= full:
            break
        crop += 1
    return crop


def _horizontal_depth(mask):
    """Per-covered-pixel horizontal distance (px) to the nearest left/right end
    of its own covered run on that row. Peaks at each row's run centre, so the
    equal-depth line between two horizontally-overlapping layers is a VERTICAL
    seam (not the diamond that a 2-D distance transform produces)."""
    m = mask > 0
    h, w = m.shape
    left = np.zeros((h, w), np.float32)
    cnt = np.zeros(h, np.float32)
    for x in range(w):
        cnt = np.where(m[:, x], cnt + 1, 0)
        left[:, x] = cnt
    right = np.zeros((h, w), np.float32)
    cnt[:] = 0
    for x in range(w - 1, -1, -1):
        cnt = np.where(m[:, x], cnt + 1, 0)
        right[:, x] = cnt
    return np.minimum(left, right) * m


def seam_weights(masks, blend_px):
    """Hard vertical-seam overlap blend with a bounded left-right transition band.

    Each pixel is owned by the layer whose coverage run it sits deepest inside
    horizontally; the equal-depth line between two overlapping layers is the HARD
    vertical seam. `blend_px` is the number of horizontal pixels discarded from
    each image's side across that seam: a layer ramps linearly from full weight
    at the seam down to 0 once it is `blend_px` shallower than the winner, then
    hard-cuts. blend_px=0 => winner-takes-all. Single coverage always stays
    weight 1 (never made translucent); disjoint regions stay clean.

    Exact ties — two layers occupying the same position with identical depth —
    are broken by LEFT priority via a sub-pixel depth bias (earlier index in
    `masks`, which extract orders left-to-right, wins). The bias is far smaller
    than one pixel, so it decides only genuine ties and never perturbs the
    normal transition band between offset neighbours."""
    D = np.stack([_horizontal_depth(mk) for mk in masks], axis=0)  # (L,H,W)
    covered = D > 0
    n = len(masks)
    # left-priority tie-break: earlier index gets a sub-pixel depth nudge so a
    # perfectly co-located pair resolves to one image instead of blending 50/50.
    bias = (np.arange(n)[::-1] * 1e-3).astype(np.float32)[:, None, None]
    Db = (D + bias) * covered
    dmax = Db.max(axis=0)
    if blend_px <= 0:
        win = np.argmax(Db, axis=0)
        w = np.zeros_like(D)
        for i in range(n):
            w[i] = (win == i) & covered[i]
    else:
        w = np.clip(1.0 - (dmax - Db) / float(blend_px), 0.0, 1.0) * covered
    total = np.maximum(w.sum(axis=0), 1e-6)
    return [(w[i] / total).astype(np.float32) for i in range(n)]


# distinct BGR hues for the fusion heatmap, one per layer (cycles if >8)
_HEAT_COLORS = [
    (60, 60, 220), (60, 200, 60), (220, 140, 40), (40, 210, 220),
    (210, 60, 200), (40, 160, 240), (200, 200, 40), (150, 90, 230),
]


def fusion_heatmap(weights, out_h, out_w):
    """Colour each layer by weight to visualise per-pixel fusion: solid colour =
    that image owns the pixel, blended colour = transition band, black = uncovered."""
    heat = np.zeros((out_h, out_w, 3), np.float32)
    for i, w in enumerate(weights):
        color = np.float32(_HEAT_COLORS[i % len(_HEAT_COLORS)])
        heat += w[..., None] * color
    return np.clip(heat, 0, 255).astype(np.uint8)


def render_stills(data_path, tex_dir, still_path, grid_path,
                  ppm=None, unit_scale=1.0, neg_v=False, target_width=640, margin=2,
                  blend_px=0.0, full_res=False, heatmap_path=None,
                  crop_bottom_px=0):
    data_path = Path(data_path)
    tex_dir = Path(tex_dir)
    if not data_path.is_file():
        raise SystemExit(f"data file does not exist: {data_path}")
    with open(str(data_path), encoding="utf-8") as f:
        loaded = json.load(f)
    if "meshes" not in loaded:
        raise SystemExit(f"data file missing 'meshes' key: {data_path}")
    meshes = loaded["meshes"]

    texs = []
    for m in meshes:
        path = tex_dir / m["texture_basename"]
        texture = cv2.imread(str(path))
        if texture is None:
            raise SystemExit(f"cannot read texture: {path}")
        texs.append(texture)

    rr.to_meters(meshes, unit_scale, neg_v)
    xmin, xmax, ymin, ymax = rr.world_bounds(meshes)
    if ppm is None:
        if full_res:
            # Output height == source image height; width scales proportionally.
            # ppm chosen so the world-Y span maps to the tallest source texture.
            src_h = max(t.shape[0] for t in texs)
            span_y = ymax - ymin
            ppm = src_h / span_y if span_y > 0 else 100.0
        else:
            ppm = resolve_ppm(xmin, xmax, target_width)
    # pad the world bounds by `margin` px so vertices exactly on the edge (or a
    # hair past it, from float rounding) never project outside the canvas — the
    # reused build_remap negative-indexes and crashes otherwise.
    pad = margin / ppm
    xmin, ymin = xmin - pad, ymin - pad
    xmax, ymax = xmax + pad, ymax + pad
    out_w = int(round((xmax - xmin) * ppm)) + 1
    out_h = int(round((ymax - ymin) * ppm)) + 1
    print(f"canvas {out_w}x{out_h} @ {ppm:.2f}px/m")

    layers = [build_remap_clipped(m, t.shape[1], t.shape[0], xmin, ymin, ppm, out_w, out_h)
              for m, t in zip(meshes, texs)]
    raw_wts = seam_weights([l[2] for l in layers], blend_px)
    wts = [w[..., None] for w in raw_wts]
    comp = rr.composite(layers, wts, [t.astype("float32") for t in texs], out_h, out_w)
    grid = (rr.draw_grid(comp.copy(), meshes, xmin, ymin, ppm, out_h)
            if grid_path is not None else None)
    heat = (fusion_heatmap(raw_wts, out_h, out_w)
            if heatmap_path is not None else None)

    # Trim ragged black rows at the bottom (perspective floor gaps of the shorter
    # planes leave uncovered pixels there) BEFORE scaling to source height, so the
    # rescale never stretches black. Only meaningful in --full-res (where we then
    # rescale to source height); an explicit --crop-bottom-px overrides the auto count.
    if full_res:
        union = np.zeros((out_h, out_w), np.uint8)
        for l in layers:
            union |= l[2]
        crop = crop_bottom_px if crop_bottom_px else bottom_dirty_rows(union)
    else:
        crop = 0
        if crop_bottom_px:
            raise SystemExit("--crop-bottom-px requires --full-res")

    if crop:
        target_height = max(t.shape[0] for t in texs)
        comp = crop_bottom_and_scale(comp, crop, target_height)
        if grid is not None:
            grid = crop_bottom_and_scale(grid, crop, target_height)
        if heat is not None:
            heat = crop_bottom_and_scale(
                heat, crop, target_height, interpolation=cv2.INTER_NEAREST)
        out_h, out_w = comp.shape[:2]
        how = "explicit" if crop_bottom_px else "auto"
        print(f"cropped bottom {crop}px ({how}) -> scaled to {out_w}x{out_h}")

    if still_path is not None:
        still_path = Path(still_path)
        still_path.parent.mkdir(parents=True, exist_ok=True)
        rr.write_image(still_path, comp, "still")
        print(f"wrote still {still_path}")
    if grid_path is not None:
        grid_path = Path(grid_path)
        grid_path.parent.mkdir(parents=True, exist_ok=True)
        rr.write_image(grid_path, grid, "grid")
        print(f"wrote grid still {grid_path}")
    if heatmap_path is not None:
        heatmap_path = Path(heatmap_path)
        heatmap_path.parent.mkdir(parents=True, exist_ok=True)
        rr.write_image(heatmap_path, heat, "heatmap")
        print(f"wrote fusion heatmap {heatmap_path}")
    return out_w, out_h


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render underwater stitch still + grid")
    ap.add_argument("--data", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_mesh.json")
    ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "underwater" / "models" / "01d.fbm")
    ap.add_argument("--still", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_stitch.png")
    ap.add_argument("--grid-still", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_grid.png")
    ap.add_argument("--ppm", type=float, default=None,
                    help="pixels per metre; default adapts world-X span to --target-width")
    ap.add_argument("--target-width", type=int, default=640)
    ap.add_argument("--unit-scale", type=float, default=1.0)
    ap.add_argument("--neg-v", dest="neg_v", action="store_true", default=False,
                    help="flip Y (world V) when compositing; off by default (upright for 01d)")
    ap.add_argument("--blend-px", type=float, default=0.0,
                    help="horizontal pixels discarded from each image's side across "
                         "the vertical seam; 0 = hard cut, larger = wider blend")
    ap.add_argument("--full-res", action="store_true",
                    help="output height = source image height, width scales "
                         "proportionally (ignores --target-width unless --ppm set)")
    ap.add_argument("--heatmap", type=Path, default=None,
                    help="also write a per-image fusion heatmap to this path")
    ap.add_argument("--crop-bottom-px", type=int, default=0,
                    help="after compositing, remove this many bottom rows and "
                         "scale proportionally back to source height; requires --full-res")
    args = ap.parse_args(argv)
    render_stills(args.data, args.tex_dir, args.still, args.grid_still,
                  ppm=args.ppm, unit_scale=args.unit_scale, neg_v=args.neg_v,
                  target_width=args.target_width, blend_px=args.blend_px,
                  full_res=args.full_res, heatmap_path=args.heatmap,
                  crop_bottom_px=args.crop_bottom_px)


if __name__ == "__main__":
    main()
