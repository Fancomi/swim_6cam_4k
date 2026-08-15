"""Pure OpenCV rendering helpers for FBX UV meshes.

The FBX loader stores UVs as normalized coordinates. This module deliberately
does not import the FBX SDK, so the coordinate conversion and drawing behavior
can be tested on a machine that only has OpenCV and NumPy installed.
"""
import cv2
import numpy as np

from python.common.media import MediaError, read_image
from python.common.paths import PROJECT_ROOT
from .meters import METER_PRECISION, label_anchors


class OverlayError(ValueError):
    """The mesh overlay options or data are invalid."""


def uv_to_pixel(uv, image_shape, v_origin="bottom"):
    """Convert one normalized ``(u, v)`` coordinate to an integer pixel.

    FBX UVs conventionally use a bottom-left origin, while OpenCV images use a
    top-left origin. Coordinates are intentionally not clipped: OpenCV can
    clip lines at the image boundary, and clipping here would change geometry
    that is legitimately just outside the camera image.
    """
    if v_origin not in {"bottom", "top"}:
        raise OverlayError("v_origin must be 'bottom' or 'top'")
    if len(image_shape) < 2:
        raise OverlayError("image_shape must contain height and width")
    height, width = int(image_shape[0]), int(image_shape[1])
    if height < 1 or width < 1:
        raise OverlayError("image_shape must have positive dimensions")
    if len(uv) != 2:
        raise OverlayError("UV coordinates must contain exactly two values")

    u, v = float(uv[0]), float(uv[1])
    if v_origin == "bottom":
        v = 1.0 - v
    return np.rint((u * (width - 1), v * (height - 1))).astype(np.int32)


def load_texture(mesh, kind="base image"):
    """The BGR image an ``extract_mesh`` dict references.

    ``mesh["texture"]`` is a repo-relative path (``display()`` output), so the
    .fbm texture dirs — which are gitignored — still resolve on this machine.
    A missing file is a data problem, not a code problem: the message says how
    to recover rather than letting the caller guess.
    """
    reference = mesh.get("texture")
    if not reference:
        raise MediaError(f"cannot read {kind}: mesh has no texture")
    path = PROJECT_ROOT / reference
    try:
        return read_image(path, kind)
    except MediaError:
        raise MediaError(
            f"cannot read {kind}: {reference} — the .fbm texture dir is "
            f"gitignored; restore it from the source asset and re-run"
        ) from None


def _triangle_pixels(triangle, image_shape, v_origin):
    if len(triangle) != 3:
        raise OverlayError("mesh triangles must contain exactly three vertices")
    return np.asarray(
        [uv_to_pixel(vertex["uv"], image_shape, v_origin) for vertex in triangle],
        dtype=np.int32,
    )


def _draw_polygons(image, polygons, color, *, thickness, fill_alpha,
                   vertex_radius):
    """Draw `polygons` (int32 Nx3x2 arrays) onto `image` in place."""
    if fill_alpha:
        filled = image.copy()
        for points in polygons:
            cv2.fillConvexPoly(filled, points, color)
        image = cv2.addWeighted(filled, float(fill_alpha), image,
                                1.0 - float(fill_alpha), 0.0)

    for points in polygons:
        cv2.polylines(image, [points], True, color, int(thickness), cv2.LINE_AA)
        if vertex_radius:
            for point in points:
                cv2.circle(image, tuple(int(value) for value in point),
                           int(vertex_radius), color, -1, cv2.LINE_AA)
    return image


def draw_mesh(image, mesh, color, *, v_origin="bottom", thickness=1,
              fill_alpha=0.0, vertex_radius=0):
    """Return a copy of ``image`` with one mesh drawn over it.

    ``mesh`` is the dictionary returned by ``fbx_tools.scene.extract_mesh``.
    ``fill_alpha`` is between 0 and 1; zero draws outlines only. The input
    image is never modified in place.
    """
    if image is None or image.ndim != 3 or image.shape[2] != 3:
        raise OverlayError("image must be a BGR color image")
    if not 1 <= int(thickness):
        raise OverlayError("thickness must be at least 1")
    if not 0.0 <= float(fill_alpha) <= 1.0:
        raise OverlayError("fill_alpha must be between 0 and 1")
    if int(vertex_radius) < 0:
        raise OverlayError("vertex_radius cannot be negative")
    if len(color) != 3:
        raise OverlayError("color must contain three BGR values")

    result = image.copy()
    triangles = mesh.get("triangles", ())
    polygons = [
        _triangle_pixels(triangle, result.shape, v_origin)
        for triangle in triangles
    ]
    return _draw_polygons(result, polygons, color,
                          thickness=thickness, fill_alpha=fill_alpha,
                          vertex_radius=vertex_radius)


def draw_meshes(image, meshes, *, v_origin="bottom", thickness=1,
                fill_alpha=0.0, vertex_radius=0,
                colors=((0, 255, 255), (0, 128, 255))):
    """Draw several meshes in order, assigning one color to each mesh."""
    if len(colors) < len(meshes):
        raise OverlayError("not enough colors for the requested meshes")
    result = image.copy()
    for index, mesh in enumerate(meshes):
        result = draw_mesh(
            result,
            mesh,
            colors[index],
            v_origin=v_origin,
            thickness=thickness,
            fill_alpha=fill_alpha,
            vertex_radius=vertex_radius,
        )
    return result


# Meter label colors come from the MESH kind, not the axis: each mesh's labels
# share its outline color (vertical cyan, surface orange) so a viewer can tell
# which grid a label belongs to at a glance. The dark backing keeps the label
# legible even where the camera image or another mesh sits behind it.
LABEL_FONT = cv2.FONT_HERSHEY_SIMPLEX
LABEL_BG_COLOR = (20, 20, 20)    # dark backing so the label reads over any mesh
LABEL_BG_ALPHA = 0.7
LABEL_FONT_SCALE = 0.7
LABEL_THICKNESS = 2
LABEL_OFFSET = 8


def _put_text(image, text, pixel, color, font_scale, thickness, offset,
              side="above"):
    """`text` anchored at `pixel`, offset clear of the mesh edge, on a dark pad.

    ``side`` says where the text sits relative to the anchor: "above" (X labels
    anchored at a mesh edge the label clears), "below" (the same but for an
    anchor at the image top, where "above" would draw off-canvas), or "left"
    (Y labels anchored at the mesh's right end so the row meters run down the
    right side). The dark backing keeps the text legible even where the camera
    image or another mesh sits behind it.
    """
    (text_width, text_height), baseline = cv2.getTextSize(
        text, LABEL_FONT, font_scale, thickness)
    if side == "above":
        origin = (int(pixel[0]) - text_width // 2,
                  int(pixel[1]) - offset - baseline)
    elif side == "below":
        origin = (int(pixel[0]) - text_width // 2,
                  int(pixel[1]) + offset)
    else:                                   # "left": to the left of the anchor
        origin = (int(pixel[0]) - offset - text_width,
                  int(pixel[1]) + text_height // 2)
    pad = 2
    top = max(0, origin[1] - text_height - pad)
    bottom = min(image.shape[0], origin[1] + baseline + pad)
    left = max(0, origin[0] - pad)
    right = min(image.shape[1], origin[0] + text_width + pad)
    if right > left and bottom > top:
        backing = image[top:bottom, left:right].astype(np.float32)
        backing = (backing * (1.0 - LABEL_BG_ALPHA)
                   + np.asarray(LABEL_BG_COLOR, np.float32) * LABEL_BG_ALPHA)
        image[top:bottom, left:right] = backing.astype(np.uint8)
    cv2.putText(image, text, origin, LABEL_FONT, font_scale, color,
                thickness, cv2.LINE_AA)


def draw_meter_labels(image, mesh, grid, *, v_origin="bottom",
                      color=(0, 255, 255),
                      font_scale=LABEL_FONT_SCALE,
                      thickness=LABEL_THICKNESS,
                      offset=LABEL_OFFSET):
    """Copy of `image` with meter labels overlaid beside the mesh's UV projection.

    Label positions come from ``meters.label_anchors`` (deduplicated, derived
    from the mesh's actual geometry), so each label lands on the gridline it
    names and a meter spanning several rows/bands is written once. An anchor
    that projects outside the camera view is skipped — no invented text beyond
    the mesh's real extent.

    All labels use the same `color`, which the caller sets to the mesh's own
    outline color, so each mesh's labels read as belonging to it. Returns a
    copy; never mutates `image`.
    """
    result = image.copy()
    if not grid:
        return result
    height, width = result.shape[:2]
    for uv, text, side in label_anchors(mesh, grid):
        pixel = uv_to_pixel(uv, result.shape, v_origin)
        if not (0 <= pixel[0] < width and 0 <= pixel[1] < height):
            continue        # outside the camera view: nothing to label
        _put_text(result, text, pixel, color,
                  font_scale, thickness, offset, side=side)
    return result
