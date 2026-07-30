"""Draw a missing calibration line back into a texture, at the FBX's own spacing.

The designer's overhead textures carry a fan of yellow lines, one every 0.5m
along the lane. One line is absent from C06.jpg, so the stitched panorama shows a
1.0m gap where every other interval is 0.5m. The gap sits inside C06's exclusive
region, so it is a hole in the source art, not a seam artefact.

Rather than eyeballing a line into the image, this derives it: fit the lines that
ARE present to a uniform world-X grid, find which grid index has no line, then
walk that world-X down the plane's world-Y extent, converting each step to
texture pixels through the mesh's own UVs. The result lands on the same
perspective the neighbouring lines follow, because it goes through the same
mapping they were drawn under.

    python -m python.stitch.patch_grid overhead
"""
import argparse

import cv2
import numpy as np

from python.common.media import read_image, write_image
from python.stitch import compose as C
from python.stitch import profiles as P
from python.stitch.profiles import StepError

# The calibration lines: saturated yellow, a few pixels wide.
HSV_LO = (20, 80, 120)
HSV_HI = (40, 255, 255)
LINE_BGR = (1, 254, 254)
LINE_WIDTH = 3
GRID_STEP_M = 0.5
# A line must cover this fraction of the canvas height to count as a grid line
# rather than a lane rope or a stray highlight.
MIN_COVERAGE = 0.25


def line_columns(image, min_coverage=MIN_COVERAGE):
    """Canvas x of every full-height yellow line, left to right."""
    mask = cv2.inRange(cv2.cvtColor(image, cv2.COLOR_BGR2HSV), HSV_LO, HSV_HI)
    height = image.shape[0]
    counts = (mask > 0).sum(axis=0)
    columns, start, inside = [], 0, False
    for x, count in enumerate(counts):
        if count > height * min_coverage and not inside:
            start, inside = x, True
        elif count <= height * min_coverage and inside:
            columns.append((start + x - 1) // 2)
            inside = False
    if inside:
        columns.append((start + len(counts) - 1) // 2)
    return columns


def missing_world_x(columns, xmin, ppm, step=GRID_STEP_M):
    """World X of every grid slot that has no line.

    The lines are a uniform `step`-metre grid, so snapping each detected line to
    the nearest slot and listing the empty slots between the first and last is
    enough — no assumption about which one is missing."""
    if len(columns) < 2:
        raise StepError(f"need at least two lines to fit a grid, found {len(columns)}")
    world = [xmin + column / ppm for column in columns]
    indices = [round((x - world[0]) / step) for x in world]
    residual = max(abs((x - world[0]) - index * step)
                   for x, index in zip(world, indices))
    if residual > step / 4:
        raise StepError(
            f"lines do not fit a {step}m grid (residual {residual * 1000:.0f}mm)")
    present = set(indices)
    return [world[0] + index * step
            for index in range(indices[0], indices[-1] + 1)
            if index not in present], residual


def uv_sampler(mesh):
    """(x, y) -> (u, v) through the mesh's triangles; None outside every one."""
    triangles = []
    for triangle in mesh["triangles"]:
        pos = np.array([vertex["pos"] for vertex in triangle], float)
        uv = np.array([vertex["uv"] for vertex in triangle], float)
        basis = np.array([[pos[1, 0] - pos[0, 0], pos[2, 0] - pos[0, 0]],
                          [pos[1, 1] - pos[0, 1], pos[2, 1] - pos[0, 1]]])
        if abs(np.linalg.det(basis)) < 1e-12:
            continue
        triangles.append((pos, uv, np.linalg.inv(basis)))

    def sample(x, y):
        for pos, uv, inverse in triangles:
            s, t = inverse @ np.array([x - pos[0, 0], y - pos[0, 1]])
            if s >= -1e-9 and t >= -1e-9 and s + t <= 1 + 1e-9:
                return uv[0] + s * (uv[1] - uv[0]) + t * (uv[2] - uv[0])
        return None

    return sample


def texture_polyline(mesh, world_x, tex_w, tex_h, samples=400):
    """The missing line as texture-pixel points, top to bottom of the plane."""
    sample = uv_sampler(mesh)
    ys = [vertex["pos"][1] for triangle in mesh["triangles"] for vertex in triangle]
    points = []
    for y in np.linspace(min(ys), max(ys), samples):
        uv = sample(world_x, y)
        if uv is None:
            continue
        points.append((uv[0] * tex_w, (1.0 - uv[1]) * tex_h))
    return np.array(points, np.float32)


def owning_mesh(meshes, world_x):
    """The mesh whose exclusive span contains `world_x`.

    A gap inside an overlap would be covered by the neighbour, so the only gaps
    worth patching are in a single plane's own region; refusing to guess when two
    planes both cover the spot keeps this from painting over a good line."""
    owners = []
    for mesh in meshes:
        xs = [vertex["pos"][0] for triangle in mesh["triangles"] for vertex in triangle]
        if min(xs) <= world_x <= max(xs):
            owners.append(mesh)
    if not owners:
        raise StepError(f"no plane covers world X {world_x:.3f}")
    if len(owners) > 1:
        names = [mesh["texture_basename"] for mesh in owners]
        raise StepError(
            f"world X {world_x:.3f} lies in an overlap ({', '.join(names)}); "
            "the neighbour already covers it, so there is nothing to patch")
    return owners[0]


def patch(profile, dry_run=False):
    """Find the gap in the stitched still, draw it into the owning texture."""
    still = profile.out_dir / "stitch.png"
    if not still.is_file():
        raise StepError(f"stitched still missing (run the still step): {still}")
    meshes = C.load_meshes(profile.mesh_json, neg_v=profile.neg_v)
    image = read_image(still, "stitched still")
    columns = line_columns(image)

    # The still's canvas origin, matching what render.py used: the world bounds
    # padded by the profile's margin.
    canvas = C.Canvas(meshes, profile.ppm, margin=profile.still_margin)
    gaps, residual = missing_world_x(columns, canvas.xmin, profile.ppm)
    print(f"{len(columns)} lines on a {GRID_STEP_M}m grid "
          f"(fit residual {residual * 1000:.1f}mm)")
    if not gaps:
        print("no gap in the grid — nothing to patch")
        return []

    written = []
    for world_x in gaps:
        mesh = owning_mesh(meshes, world_x)
        texture_path = profile.tex_dir / mesh["texture_basename"]
        texture = read_image(texture_path, "texture")
        height, width = texture.shape[:2]
        points = texture_polyline(mesh, world_x, width, height)
        if len(points) < 2:
            raise StepError(f"world X {world_x:.3f} maps to no texture span")
        print(f"  gap at world X {world_x:.3f} -> {mesh['texture_basename']} "
              f"({points[0][0]:.0f},{points[0][1]:.0f}).."
              f"({points[-1][0]:.0f},{points[-1][1]:.0f})")
        if dry_run:
            continue
        cv2.polylines(texture, [np.int32(points)], False, LINE_BGR,
                      LINE_WIDTH, cv2.LINE_AA)
        written.append(write_image(texture_path, texture, "patched texture"))
    if written:
        print(f"patched {len(written)} texture(s); rerun the still step to see it")
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Draw a missing calibration line into its source texture")
    parser.add_argument("line", nargs="?", default="overhead",
                        choices=P.names(),
                        help="camera line to patch (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the gap and where it would be drawn, "
                             "without touching the texture")
    args = parser.parse_args(argv)
    try:
        patch(P.get(args.line), dry_run=args.dry_run)
    except StepError as error:
        raise SystemExit(f"error: {error}")


if __name__ == "__main__":
    main()
