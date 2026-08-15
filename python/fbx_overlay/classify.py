"""Classify FBX UV meshes by the image region they cover.

A camera FBX in this chain carries three meshes: the full-frame quad whose
texture IS the camera image, the vertical water-plane mesh (the lane wall,
seen from the side), and the water-surface mesh (a horizontal band at the
bottom of the side view). This module tells them apart purely from UV stats,
so the CLI does not hard-code node names — the artist can rename or rebuild
the geometry and the classification still holds.

The thresholds are data-driven, measured from the femto/gemini models:

    full-frame quad  2 tris, du = dv = 1.0        (a [0,1]² texture plane)
    vertical water   126-140 tris, dv ≈ 0.40-0.50 (bottom into mid image)
    water surface    24-30 tris,  dv ≈ 0.12-0.14  (a thin horizontal band)

dv is the UV-v span; 0.3 sits cleanly between the two groups.
"""
from enum import Enum


class MeshKind(str, Enum):
    FULL_FRAME = "full_frame"
    VERTICAL = "vertical"
    SURFACE = "surface"
    PLANE = "plane"              # 俯视泳道平面（overhead 相机），非水面带


# A full-frame quad is at most a single polygon (fan-triangulated to 2 tris).
# The threshold of 4 tolerates a quad split into two triangles plus slack.
FULL_FRAME_MAX_TRIANGLES = 4
# The quad spans the whole [0,1]² UV space; small overshoot past 1.0 (measured
# up to 1.002) must not disqualify it.
FULL_FRAME_DU = 0.9
FULL_FRAME_DV = 0.9
# Measured vertical water dv≈0.40-0.50 vs water surface dv≈0.12-0.14.
DV_VERTICAL_MIN = 0.3
# The overhead planes are large (200/340 tris) and start mid-UV vertically
# (v_min 0.42-0.46), unlike the water surface band which touches the image
# bottom (v_min ≈ 0). v_min guard keeps a future *dense* bottom band from
# flipping to PLANE.
PLANE_MIN_TRIANGLES = 100
PLANE_V_MIN = 0.2


def _uv_spans(triangles):
    """(du, dv, v_min) of the mesh's UV bounding box over every vertex."""
    lows = [None, None]
    highs = [None, None]
    for triangle in triangles:
        for vertex in triangle:
            u, v = vertex["uv"]
            if lows[0] is None or u < lows[0]:
                lows[0] = u
            if highs[0] is None or u > highs[0]:
                highs[0] = u
            if lows[1] is None or v < lows[1]:
                lows[1] = v
            if highs[1] is None or v > highs[1]:
                highs[1] = v
    if lows[0] is None:
        return 0.0, 0.0, 0.0
    return highs[0] - lows[0], highs[1] - lows[1], lows[1]


def classify_mesh(mesh):
    """MeshKind for an ``extract_mesh`` dict.

    A small mesh (<= 4 triangles) is either the full-frame camera quad or a
    degenerate sliver; anything else is split by how much of the image height
    its UV-v spans. A thin-v mesh that is large AND starts mid-image is an
    overhead plane seen from above; a thin-v mesh touching the bottom edge is
    the water surface band of the side view.
    """
    triangles = mesh.get("triangles", ())
    du, dv, v_min = _uv_spans(triangles)
    if len(triangles) <= FULL_FRAME_MAX_TRIANGLES:
        if du >= FULL_FRAME_DU and dv >= FULL_FRAME_DV:
            return MeshKind.FULL_FRAME
        return MeshKind.VERTICAL
    if dv >= DV_VERTICAL_MIN:
        return MeshKind.VERTICAL
    if v_min >= PLANE_V_MIN and len(triangles) >= PLANE_MIN_TRIANGLES:
        return MeshKind.PLANE
    return MeshKind.SURFACE
