"""FBX -> mesh JSON for one line.

Ordering is a profile field, not a default: the plane lines sort left-to-right by
world X so camera identity follows position, while pool keeps the FBX's declared
order because its six meshes sit in two rows and sorting by X would interleave
the banks.

The one place this chain reaches outside itself: a line with `lane_meters` set
has its gridlines annotated with real-world metres by ``python.fbx_overlay``.
Those rules (which column is 0 m, whether a row is skipped) are calibration
knowledge, not stitching, and `classify.py`/`meters.py` are pure — no FBX SDK, no
OpenCV. Importing them beats a second copy of the rules, and beats a second
mesh.json sitting beside this one saying almost the same thing.
"""
import json

from python.common.paths import display
from python.fbx_overlay.meters import annotate_meshes
from python.fbx_tools import scene as F
from python.stitch.profiles import StepError


def _min_x(mesh):
    xs = [v["pos"][0] for t in mesh["triangles"] for v in t]
    return min(xs) if xs else float("inf")


def sort_by_world_x(meshes):
    """Meshes by ascending minimum world X (left to right); empties last."""
    return sorted(meshes, key=_min_x)


def _span(mesh, axis):
    values = [v["pos"][axis] for t in mesh["triangles"] for v in t]
    return (min(values), max(values)) if values else (float("inf"), float("-inf"))


def select_planes(meshes, band=(-11.6, -8.0), min_height=2.5):
    """Keep exactly one full-height pool plane per texture.

    A clean file has one plane per texture; all.fbx carries the 16 real planes
    plus clutter — untextured rigging frames, and per texture a set of lane-marker
    strips near Y≈0 plus alternate copies. The real swimming plane is the tall
    mesh whose world-Y extent falls inside the pool `band`; among candidates
    sharing a texture the one with the most triangles wins. Untextured meshes are
    dropped."""
    low, high = band
    best = {}
    for mesh in meshes:
        texture = mesh["texture_basename"]
        if not texture:
            continue
        y0, y1 = _span(mesh, 1)
        if y0 < low or y1 > high or (y1 - y0) <= min_height:
            continue
        current = best.get(texture)
        if current is None or len(mesh["triangles"]) > len(current["triangles"]):
            best[texture] = mesh
    return list(best.values())


def extract(profile, dst=None):
    """Read the profile's FBX and write its mesh JSON. Returns the meshes."""
    dst = dst or profile.mesh_json
    if not profile.fbx.is_file():
        raise StepError(f"FBX does not exist: {profile.fbx}")
    if not profile.tex_dir.is_dir():
        raise StepError(f"texture directory does not exist: {profile.tex_dir}")

    manager, _scene, nodes = F.read_scene(profile.fbx)
    try:
        meshes = [F.extract_mesh(node, profile.tex_dir) for node in nodes]
    finally:
        manager.Destroy()

    if profile.planes_only:
        meshes = select_planes(meshes)
        if not meshes:
            raise StepError(f"no pool plane found in {profile.fbx}")
    if profile.order == "world_x":
        meshes = sort_by_world_x(meshes)
    if len(meshes) != len(profile.camera_ids):
        raise StepError(
            f"{profile.name}: {len(meshes)} meshes for "
            f"{len(profile.camera_ids)} cameras in {profile.fbx}")

    # Metres go in the same file as the geometry: the algorithm side wants one
    # document per line, and a vertex's metre is a property of that vertex.
    if profile.lane_meters:
        annotate_meshes(meshes)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps({"source": display(profile.fbx), "meshes": meshes}),
                   encoding="utf-8")
    for camera, mesh in zip(profile.camera_ids, meshes):
        print(f"  {camera:10s} <- {mesh['node']:12s} tris={len(mesh['triangles']):4d} "
              f"tex={mesh['texture_basename']}"
              + (f" kind={mesh['kind']}" if profile.lane_meters else ""))
    print(f"wrote {dst}")
    return meshes
