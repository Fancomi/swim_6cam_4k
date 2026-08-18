"""Apply an alignment to a mesh's UVs.

The correction goes on the UVs, never on the positions. A drifted camera did not
move the pool: the world geometry, and therefore every metre annotation derived
from it, is exactly as calibrated. What changed is which pixel of the new frame
each vertex should sample — so `uv` is the only field this touches, and the
canvas, the remap tables and the seam blending downstream need no knowledge that
an alignment happened at all.

One convention conversion lives here. `aligner` works in image coordinates (y
down from the top, as OpenCV and phase correlation see it) while FBX UVs are
bottom-origin (v=0 is the image bottom, which is what `stitch.compose.build_remap`
and `fbx_overlay.render.uv_to_pixel` both assume). So v flips going in and coming
back out. Getting that backwards produces a correction of the right magnitude in
the wrong vertical direction, which reads as "the alignment made it worse" rather
than as a sign error.
"""
import copy

import cv2
import numpy as np


def warp_uv(mesh, matrix):
    """A copy of `mesh` with its UVs corrected by a normalised 3x3.

    `matrix` is `Alignment.H` — normalised calibration-image coordinates to
    normalised new-image coordinates. UVs are not clamped to [0,1]: a vertex
    legitimately sits outside the frame (the underwater planes overhang their
    images by design), and both consumers already handle out-of-range UVs —
    `build_remap` by clipping when the profile says to, the overlay renderer by
    letting OpenCV clip the drawing."""
    matrix = np.asarray(matrix, dtype=np.float64)
    warped = copy.deepcopy(mesh)
    for triangle in warped["triangles"]:
        # float64 throughout: float32 points cost ~3e-9 per coordinate, which is
        # invisible in a rendered pixel but means an identity transform does not
        # round-trip exactly, and a no-op that changes the data is a bad no-op.
        points = np.array([[[vertex["uv"][0], 1.0 - vertex["uv"][1]]]
                           for vertex in triangle], dtype=np.float64)
        moved = cv2.perspectiveTransform(points, matrix).reshape(-1, 2)
        for vertex, (x, y) in zip(triangle, moved):
            vertex["uv"][0] = float(x)
            vertex["uv"][1] = float(1.0 - y)
    return warped


def warp_meshes(meshes, alignments):
    """`meshes` with each one warped by its alignment, where one was accepted.

    `alignments` is parallel to `meshes`; None, or an alignment that was not
    accepted, leaves that mesh exactly as calibrated. Falling back per camera
    rather than per line is the point: fifteen good corrections should not be
    thrown away because the sixteenth camera's view was too disturbed to
    register."""
    if alignments is None:
        return list(meshes)
    if len(alignments) != len(meshes):
        raise ValueError(f"{len(alignments)} alignments for {len(meshes)} meshes")
    return [warp_uv(mesh, alignment.H)
            if alignment is not None and alignment.accepted else mesh
            for mesh, alignment in zip(meshes, alignments)]


def uv_shift_px(before, after, size):
    """(mean, median, max) UV movement in pixels, for reporting a correction.

    The pair of meshes must share a vertex order, which they do when `after` came
    from `warp_uv(before, ...)`. Also used the other way round — against a
    hand-recalibrated mesh of the same geometry — to measure a correction against
    ground truth."""
    width, height = size

    def pixels(mesh):
        return np.array([[vertex["uv"][0] * width,
                          (1.0 - vertex["uv"][1]) * height]
                         for triangle in mesh["triangles"]
                         for vertex in triangle], dtype=np.float64)

    first, second = pixels(before), pixels(after)
    if first.shape != second.shape:
        raise ValueError(f"vertex counts differ: {first.shape[0]} vs "
                         f"{second.shape[0]}")
    distance = np.linalg.norm(second - first, axis=1)
    return (float(distance.mean()), float(np.median(distance)),
            float(distance.max()))
