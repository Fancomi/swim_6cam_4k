"""The stitch itself: mesh JSON in, one composited canvas out.

Every line — pool's two rows, the underwater 16, the overhead 2 — goes through
these same five steps: project the mesh to canvas pixels, build a per-lane remap
table, weight the overlaps, remap each source, accumulate. What differs is
declared in profiles.py, never branched on here.

The remap table is built once because the geometry is constant for a whole clip;
per frame it is only cv2.remap plus a weighted add.
"""
import json
from pathlib import Path

import cv2
import numpy as np

from python.stitch.profiles import StepError


def load_meshes(path, neg_v=False, unit_scale=1.0, neg_u=False):
    """Mesh list from `path`, already scaled to metres."""
    path = Path(path)
    if not path.is_file():
        raise StepError(f"mesh JSON missing (run the extract step): {path}")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if "meshes" not in loaded:
        raise StepError(f"mesh JSON has no 'meshes' key: {path}")
    meshes = loaded["meshes"]
    to_metres(meshes, unit_scale, neg_v, neg_u)
    return meshes


def to_metres(meshes, unit_scale=1.0, neg_v=False, neg_u=False):
    """Scale positions in place, optionally mirroring either world axis.

    `neg_v` flips world Y: the pool bake stores it increasing downwards, so it
    needs the flip, while the plane lines are modelled upright and must not be
    flipped.

    `neg_u` flips world X. It exists so a file modelled with the pool rotated
    180° lands on the same axes as the rest — set both and the mesh comes in
    already aligned, which keeps the rotation a property of the file (one profile
    field) instead of a post-processing step every consumer would have to repeat.
    """
    for mesh in meshes:
        for triangle in mesh["triangles"]:
            for vertex in triangle:
                x = -vertex["pos"][0] if neg_u else vertex["pos"][0]
                y = -vertex["pos"][1] if neg_v else vertex["pos"][1]
                vertex["pos"][0] = x / unit_scale
                vertex["pos"][1] = y / unit_scale


def world_bounds(meshes):
    """(xmin, xmax, ymin, ymax) across every vertex."""
    xs = [v["pos"][0] for m in meshes for t in m["triangles"] for v in t]
    ys = [v["pos"][1] for m in meshes for t in m["triangles"] for v in t]
    return min(xs), max(xs), min(ys), max(ys)


class Canvas:
    """Output raster geometry: where world metres land in canvas pixels.

    `margin` pads the world bounds so a vertex sitting exactly on the edge — or a
    hair past it after float rounding — still projects inside the raster;
    build_remap would otherwise index negatively. The pool line uses margin 0
    because its published still size (5001x2101) matches the .swasset canvas,
    and it has never rounded outside.
    """

    __slots__ = ("xmin", "ymin", "ppm", "width", "height")

    def __init__(self, meshes, ppm, margin=0):
        xmin, xmax, ymin, ymax = world_bounds(meshes)
        pad = margin / ppm
        self.xmin, self.ymin, self.ppm = xmin - pad, ymin - pad, ppm
        self.width = int(round((xmax - xmin + 2 * pad) * ppm)) + 1
        self.height = int(round((ymax - ymin + 2 * pad) * ppm)) + 1

    @property
    def shape(self):
        return self.height, self.width

    def project(self, triangle, dtype=np.float32):
        """One triangle's vertices as canvas pixels, y down from the top."""
        return np.array([[(v["pos"][0] - self.xmin) * self.ppm,
                          self.height - 1 - (v["pos"][1] - self.ymin) * self.ppm]
                         for v in triangle], dtype)

    def __repr__(self):
        return f"Canvas({self.width}x{self.height} @ {self.ppm:.2f}px/m)"


def adaptive_ppm(meshes, source_height, target_width=640):
    """Pixels per metre when a profile does not pin one.

    With a source height, match it so the output is native scale vertically;
    otherwise fit the world-X span into `target_width` for a quick look."""
    xmin, xmax, ymin, ymax = world_bounds(meshes)
    if source_height:
        span = ymax - ymin
        return source_height / span if span > 0 else 100.0
    span = xmax - xmin
    return target_width / span if span > 0 else 100.0


def build_remap(mesh, canvas, tex_size, clip=False):
    """Inverse map (canvas pixel -> source pixel) plus a coverage mask.

    `clip` is what separates pool's feather from the plane lines' seam. Off, every
    rasterised pixel counts as covered and cv2.remap border-reflects the
    out-of-range UVs, which paints a mirrored strip of the neighbour's edge right
    at each block's border. On, a pixel counts only when its source coordinate
    lands inside `tex_size`, so a block stops at its real image extent and the
    neighbour takes over cleanly.

    The pool line leaves it off: its meshes overlap broadly and blend by
    distance, so clipping at the image edge would cut into the feather.
    """
    height, width = canvas.shape
    mapx = np.zeros((height, width), np.float32)
    mapy = np.zeros((height, width), np.float32)
    mask = np.zeros((height, width), np.uint8)
    tex_w, tex_h = tex_size
    for triangle in mesh["triangles"]:
        dst = canvas.project(triangle)
        src = np.array([[v["uv"][0] * tex_w, (1.0 - v["uv"][1]) * tex_h]
                        for v in triangle], np.float32)
        x, y, w, h = cv2.boundingRect(dst)
        if w <= 0 or h <= 0:
            continue
        local = (dst - np.float32([x, y])).astype(np.float32)
        affine = cv2.getAffineTransform(local, src)
        grid_y, grid_x = np.mgrid[0:h, 0:w]
        grid_x = grid_x.astype(np.float32)
        grid_y = grid_y.astype(np.float32)
        sx = affine[0, 0] * grid_x + affine[0, 1] * grid_y + affine[0, 2]
        sy = affine[1, 0] * grid_x + affine[1, 1] * grid_y + affine[1, 2]
        filled = np.zeros((h, w), np.uint8)
        cv2.fillConvexPoly(filled, np.int32(local), 1)
        inside = filled > 0
        if clip:
            inside &= ((sx >= 0) & (sx <= tex_w - 1)
                       & (sy >= 0) & (sy <= tex_h - 1))
        mapx[y:y + h, x:x + w][inside] = sx[inside]
        mapy[y:y + h, x:x + w][inside] = sy[inside]
        mask[y:y + h, x:x + w][inside] = 1
    m1, m2 = cv2.convertMaps(mapx, mapy, cv2.CV_16SC2)
    return m1, m2, mask


def feather_weights(masks):
    """Distance-to-own-boundary blend, normalised per pixel.

    Single-coverage pixels keep weight 1 so edges do not darken; overlaps ramp
    smoothly; disjoint regions (the pool's centre line) stay a hard cut for
    free. This is the pool bank blend: its two rows meet at an angle, so there is
    no single seam direction to pick."""
    distances = []
    for mask in masks:
        padded = cv2.copyMakeBorder(mask, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=1)
        distance = cv2.distanceTransform(padded, cv2.DIST_L2, 3)[1:-1, 1:-1]
        distances.append(distance * (mask > 0))
    total = np.maximum(sum(distances), 1e-6)
    return [(d / total).astype(np.float32) for d in distances]


def _horizontal_depth(mask):
    """Distance along each row to the nearest end of that pixel's covered run.

    Peaks at each run's centre, so the equal-depth line between two
    horizontally-overlapping lanes is a straight VERTICAL seam — a 2-D distance
    transform would instead put a diamond there."""
    covered = mask > 0
    height, width = covered.shape
    left = np.zeros((height, width), np.float32)
    running = np.zeros(height, np.float32)
    for x in range(width):
        running = np.where(covered[:, x], running + 1, 0)
        left[:, x] = running
    right = np.zeros((height, width), np.float32)
    running[:] = 0
    for x in range(width - 1, -1, -1):
        running = np.where(covered[:, x], running + 1, 0)
        right[:, x] = running
    return np.minimum(left, right) * covered


def seam_weights(masks, blend_px):
    """Vertical-seam blend with a bounded left-right transition band.

    Each pixel belongs to the lane whose covered run it sits deepest inside; the
    equal-depth line between two overlapping lanes is the seam. `blend_px` is how
    many horizontal pixels each side gives up across it: a lane ramps from full
    weight at the seam to zero once it is `blend_px` shallower than the winner,
    then hard-cuts. 0 is winner-takes-all. Single coverage always stays weight 1.

    Exact ties — two lanes at identical depth — resolve to the LEFT one via a
    sub-pixel depth bias. Without it a pair of co-located planes blends 50/50
    into a ghost; the bias is far below one pixel, so it never perturbs the
    normal band between offset neighbours."""
    depth = np.stack([_horizontal_depth(mask) for mask in masks])
    covered = depth > 0
    count = len(masks)
    bias = (np.arange(count)[::-1] * 1e-3).astype(np.float32)[:, None, None]
    biased = (depth + bias) * covered
    if blend_px <= 0:
        winner = np.argmax(biased, axis=0)
        weights = np.zeros_like(depth)
        for index in range(count):
            weights[index] = (winner == index) & covered[index]
    else:
        deepest = biased.max(axis=0)
        weights = np.clip(1.0 - (deepest - biased) / float(blend_px), 0.0, 1.0) * covered
    total = np.maximum(weights.sum(axis=0), 1e-6)
    return [(weights[index] / total).astype(np.float32) for index in range(count)]


def blend_weights(masks, blend_px):
    """Per-lane weights: seam blend when `blend_px` is a number, else feather.

    One switch instead of two call paths — the asset compiler used to branch on
    it twice, once for the mask and once for the weights, and they could disagree."""
    if blend_px is None:
        return feather_weights(masks)
    return seam_weights(masks, blend_px)


def composite(layers, weights, frames, canvas):
    """Remap every source through its layer and accumulate weighted."""
    accumulator = np.zeros((canvas.height, canvas.width, 3), np.float32)
    for (m1, m2, _mask), weight, frame in zip(layers, weights, frames):
        warped = cv2.remap(frame, m1, m2, cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REFLECT_101)
        accumulator += warped.astype(np.float32) * weight
    return np.clip(accumulator, 0, 255).astype(np.uint8)


def union_coverage(layers, canvas):
    """Pixels any lane paints; the input to bottom-crop measurement."""
    union = np.zeros(canvas.shape, np.uint8)
    for layer in layers:
        union |= layer[2]
    return union


def bottom_dirty_rows(coverage):
    """Bottom rows that are not covered as widely as the widest row.

    The shorter planes' perspective floor leaves ragged black gaps along the
    bottom. The reference is the per-canvas maximum rather than the full width
    because a constant margin keeps a few zero columns even in a full row."""
    per_row = (coverage > 0).sum(axis=1)
    widest = int(per_row.max())
    crop = 0
    for row in range(coverage.shape[0] - 1, -1, -1):
        if per_row[row] >= widest:
            break
        crop += 1
    return crop


def crop_and_scale(image, crop_px, target_height, interpolation=cv2.INTER_LINEAR):
    """Drop `crop_px` bottom rows, then scale proportionally to `target_height`."""
    if crop_px < 0 or crop_px >= image.shape[0]:
        raise ValueError(f"crop_px must be in [0, {image.shape[0] - 1}]")
    cropped = image[:-crop_px] if crop_px else image
    if cropped.shape[0] == target_height:
        return cropped
    scale = target_height / cropped.shape[0]
    width = int(round(cropped.shape[1] * scale))
    return cv2.resize(cropped, (width, target_height), interpolation=interpolation)


# Distinct BGR hues, one per lane; cycles beyond eight. Shared by the grid
# overlay and the fusion heatmap so lane i is the same colour in both — they used
# to carry separate palettes, and cross-reading the two diagnostics meant
# translating between them.
LANE_COLOURS = [
    (60, 60, 220), (60, 200, 60), (220, 140, 40), (40, 210, 220),
    (210, 60, 200), (40, 160, 240), (200, 200, 40), (150, 90, 230),
]


def draw_grid(image, meshes, canvas):
    """Overlay each mesh's triangle edges and its region outline.

    Projected exactly the way build_remap does, so a misalignment in the diagnostic
    is a real misalignment and not a second projection's rounding."""
    for index, mesh in enumerate(meshes):
        colour = LANE_COLOURS[index % len(LANE_COLOURS)]
        region = np.zeros(canvas.shape, np.uint8)
        for triangle in mesh["triangles"]:
            dst = canvas.project(triangle, np.int32)
            cv2.polylines(image, [dst], True, colour, 1, cv2.LINE_AA)
            cv2.fillConvexPoly(region, dst, 1)
        contours, _ = cv2.findContours(region, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(image, contours, -1, colour, 3, cv2.LINE_AA)
    return image


def fusion_heatmap(weights, canvas):
    """Colour each lane by weight: solid means it owns the pixel, blended means
    the transition band, black means uncovered."""
    heat = np.zeros((canvas.height, canvas.width, 3), np.float32)
    for index, weight in enumerate(weights):
        heat += weight[..., None] * np.float32(LANE_COLOURS[index % len(LANE_COLOURS)])
    return np.clip(heat, 0, 255).astype(np.uint8)
