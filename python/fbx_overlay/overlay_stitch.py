"""World-projected overlay rendering for the overhead planes.

The overhead camera looks straight down at the pool, so its two planes are
stitched into one panorama canvas (the stitch chain's Canvas, world metres ->
pixels) and the mesh grid + meter labels are overlaid on that canvas — NOT on
a camera image via UV. This mirrors the stitch renderer's projection so the
overlay and the stitch output share the same world->pixel mapping.

The meter labels match the designer's lane-schematic reference
(``inputs/overhead/ label_line.png``): mesh edges in red (the lane lines) and
meter ticks in green (the distance marks).
"""
from pathlib import Path

import cv2
import numpy as np

from python.common.media import read_image, write_image
from python.stitch import compose as C

from .meters import label_anchors_world
from .render import _put_text


EDGE_COLOR = (0, 0, 255)         # red — matches the reference lane lines
LABEL_COLOR = (0, 255, 0)        # green — matches the reference distance ticks


def build_canvas(profile, meshes, tex_dir, tex_names):
    """Stitch `meshes` into one canvas: (canvas, layers, weights, composite).

    Reuses the stitch pipeline (remap per mesh, blend, accumulate) so the
    overhead panorama is byte-for-byte the same projection the runtime uses.
    """
    canvas = C.Canvas(meshes, profile.ppm, margin=profile.still_margin)
    textures = [read_image(tex_dir / name, "texture") for name in tex_names]
    layers = [
        C.build_remap(mesh, canvas, (texture.shape[1], texture.shape[0]),
                      clip=profile.clip_uv)
        for mesh, texture in zip(meshes, textures)
    ]
    weights = C.blend_weights([layer[2] for layer in layers], profile.blend_px)
    composite = C.composite(layers, [w[..., None] for w in weights],
                            [t.astype("float32") for t in textures], canvas)
    return canvas, layers, weights, composite


def draw_mesh_world(image, mesh, canvas, color, *, thickness=1):
    """Draw one mesh's triangle edges projected world->canvas onto `image`."""
    for triangle in mesh["triangles"]:
        dst = canvas.project(triangle, np.int32)
        cv2.polylines(image, [dst], True, color, int(thickness), cv2.LINE_AA)
    return image


def _gridlines(meshes):
    """(columns, rows) world gridlines across all meshes, deduplicated.

    The overhead planes are regular grids (one X per column, one Y per row),
    so the overlay draws clean gridlines — not every triangle edge — which is
    what the lane-schematic reference shows. Columns/rows shared by both planes
    (the overlap) are drawn once.
    """
    columns = {}
    rows = {}
    for mesh in meshes:
        for triangle in mesh["triangles"]:
            for vertex in triangle:
                x = round(vertex["pos"][0], 3)
                y = round(vertex["pos"][1], 3)
                columns.setdefault(x, []).append(vertex["pos"])
                rows.setdefault(y, []).append(vertex["pos"])
    return columns, rows


def draw_gridlines(image, meshes, canvas, color, *, thickness=1):
    """Overlay the meshes' world gridlines (columns + rows) on `image`."""
    columns, rows = _gridlines(meshes)
    result = image.copy()
    for x, points in columns.items():
        ys = [p[1] for p in points]
        if not ys:
            continue
        p0 = canvas.point(x, min(ys))
        p1 = canvas.point(x, max(ys))
        cv2.line(result, p0, p1, color, int(thickness), cv2.LINE_AA)
    for y, points in rows.items():
        xs = [p[0] for p in points]
        if not xs:
            continue
        p0 = canvas.point(min(xs), y)
        p1 = canvas.point(max(xs), y)
        cv2.line(result, p0, p1, color, int(thickness), cv2.LINE_AA)
    return result


def draw_canvas_overlay(image, meshes, canvas, grids, *,
                        edge_color=EDGE_COLOR, label_color=LABEL_COLOR,
                        gridlines=True):
    """Overlay mesh gridlines + world-projected meter labels on the composite.

    Each mesh's grid labels are projected via the canvas; Y labels are
    deduplicated across meshes (both planes share the same lane rows, so the
    row meters print once at the rightmost plane).
    """
    result = image.copy()
    if gridlines:
        result = draw_gridlines(result, meshes, canvas, edge_color)
    else:
        for mesh in meshes:
            draw_mesh_world(result, mesh, canvas, edge_color)
    seen_y_meters = set()
    for mesh in meshes:
        grid = grids[mesh["node"]]
        for world_x, world_y, text, side in label_anchors_world(mesh, grid):
            if side == "left":
                key = (text, round(world_y, 3))
                if key in seen_y_meters:
                    continue
                seen_y_meters.add(key)
            pixel = canvas.point(world_x, world_y)
            if not (0 <= pixel[0] < canvas.width
                    and 0 <= pixel[1] < canvas.height):
                continue
            # X labels anchor at the column's TOP edge, which on the overhead
            # canvas is the very top of the image — "above" would draw
            # off-canvas, so draw them below the anchor (inside the mesh).
            draw_side = "below" if side == "above" else side
            _put_text(result, text, pixel, label_color, 0.6, 2, 6,
                      side=draw_side)
    return result


def draw_label_line_compare(image, label_line_path, out_path):
    """Write a side-by-side comparison of the overlay and the reference.

    The reference is resized to the canvas size (aspect ratios match within
    0.4%), so the two panels share the same lane geometry and the mesh grid
    can be checked against the designer's schematic.
    """
    reference = read_image(label_line_path, "label line")
    reference = cv2.resize(reference, (image.shape[1], image.shape[0]),
                           interpolation=cv2.INTER_LINEAR)
    compare = np.concatenate([image, reference], axis=1)
    write_image(out_path, compare, "label line compare")
    return compare


def resolve_texture_set(profile, set_name=None):
    """(tex_dir, tex_names, set_name) for the requested or first texture set."""
    sets = profile.texture_sets
    if not sets:
        raise ValueError(f"profile {profile.name} has no texture sets")
    if set_name is None:
        set_name = sets[0][0]
    for name, directory, filenames in sets:
        if name == set_name:
            return Path(directory), tuple(filenames), name
    raise ValueError(f"unknown texture set {set_name!r}; "
                     f"valid sets: {', '.join(s[0] for s in sets)}")
