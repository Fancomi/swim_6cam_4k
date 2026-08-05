"""Pure OpenCV rendering helpers for FBX UV meshes.

The FBX loader stores UVs as normalized coordinates. This module deliberately
does not import the FBX SDK, so the coordinate conversion and drawing behavior
can be tested on a machine that only has OpenCV and NumPy installed.
"""
import cv2
import numpy as np


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


def _triangle_pixels(triangle, image_shape, v_origin):
    if len(triangle) != 3:
        raise OverlayError("mesh triangles must contain exactly three vertices")
    return np.asarray(
        [uv_to_pixel(vertex["uv"], image_shape, v_origin) for vertex in triangle],
        dtype=np.int32,
    )


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
    if fill_alpha:
        filled = result.copy()
        for points in polygons:
            cv2.fillConvexPoly(filled, points, color)
        result = cv2.addWeighted(filled, float(fill_alpha), result,
                                 1.0 - float(fill_alpha), 0.0)

    for points in polygons:
        cv2.polylines(result, [points], True, color, int(thickness), cv2.LINE_AA)
        if vertex_radius:
            for point in points:
                cv2.circle(result, tuple(int(value) for value in point),
                           int(vertex_radius), color, -1, cv2.LINE_AA)
    return result


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
