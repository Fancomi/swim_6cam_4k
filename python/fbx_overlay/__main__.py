"""Draw one line's FBX meshes over its camera image, with real-world metres.

Lines mirror the stitch chain's model — a rebuilt FBX of the same cameras is a
new line with a ``2`` suffix. Two exist, the water-entry cameras::

    .venv/bin/python -m python.fbx_overlay                  # both lines
    .venv/bin/python -m python.fbx_overlay --line water_entry2
    .venv/bin/python -m python.fbx_overlay --line water_entry2 --camera gemini
    .venv/bin/python -m python.fbx_overlay --camera femto   # compat: maps to water_entry2

Each sub-camera's FBX contains every mesh for that camera — the full-frame quad
whose texture IS the camera image, plus the vertical water-plane and the water
surface. The base image comes out of the FBX, so no image argument is needed.

Products per camera: one composite, one image per mesh, and ``mesh.json`` — the
full geometry with each vertex's real-world metres (see meters.py), which is what
the algorithm side consumes. Meshes are classified automatically (classify.py)
and drawn in per-kind colours with metre labels; ``--no-labels`` turns the text
off and the JSON is still written.

The overhead planes are NOT here: seen from straight above they have no camera
image to draw on, and they are stitch lines. Their metres land in that chain's
one document per line — ``python -m python.stitch overhead2 extract`` writes
``outputs/overhead2/mesh.json`` with the same annotation this package supplies
(`lane_meters`), and ``still`` draws the panorama.

Use ``--mesh FBX NODE`` to draw one explicit mesh from any FBX (regression path).
"""
import argparse
import json
from pathlib import Path

from python.common.media import MediaError, read_image, write_image
from python.common.paths import OUTPUTS, display
from python.fbx_tools import scene as fbx_scene

from .classify import MeshKind, classify_mesh
from .meters import annotate_document, grid_annotation
from .profiles import (PROFILES, get as get_profile,
                       line_for_camera, names as profile_names)
from .render import (OverlayError, draw_meshes, draw_meter_labels,
                     load_texture)


# Colors by mesh kind (BGR), matching the old two-mesh overlay so the lane
# wall and the water band read the same as before the femto/gemini split.
KIND_COLORS = {
    MeshKind.VERTICAL: (0, 255, 255),   # cyan — the lane wall
    MeshKind.SURFACE: (0, 128, 255),    # orange — the water band
}


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--line", action="append", choices=profile_names(),
        help="line to render; may be repeated (default: every line)",
    )
    parser.add_argument(
        "--camera", action="append",
        help="sub-camera to render within a line; may be repeated "
             "(e.g. femto, gemini, water_entry_a), or a line name for the "
             "compat shim",
    )
    parser.add_argument(
        "--image", type=Path, default=None,
        help="override the base image for every camera "
             "(default: the camera image extracted from the FBX)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="output directory (default: outputs/<line>/overlay)",
    )
    parser.add_argument(
        "--mesh", action="append", nargs=2, metavar=("FBX", "NODE"),
        type=str, help="FBX path and exact mesh node name; may be repeated; "
                       "overrides --line/--camera",
    )
    parser.add_argument("--uv-v-origin", choices=("bottom", "top"),
                        default="bottom")
    parser.add_argument("--line-thickness", type=int, default=2)
    parser.add_argument("--fill-alpha", type=float, default=0.0)
    parser.add_argument("--vertex-radius", type=int, default=0)
    parser.add_argument(
        "--no-labels", action="store_true",
        help="skip meter labels on the rendered overlays (mesh.json is still "
             "written)",
    )
    return parser


def _load_mesh(path, node_name):
    """One named mesh from one FBX, for the explicit --mesh regression path."""
    path = Path(path)
    if not path.is_file():
        raise OverlayError(f"FBX does not exist: {path}")
    texture_dir = path.with_suffix(".fbm")
    manager, _scene, nodes = fbx_scene.read_scene(path)
    try:
        matches = [node for node in nodes if node.GetName() == node_name]
        if len(matches) != 1:
            available = ", ".join(node.GetName() for node in nodes) or "<none>"
            raise OverlayError(
                f"expected exactly one node {node_name!r} in {path}; "
                f"available nodes: {available}"
            )
        return fbx_scene.extract_mesh(matches[0], texture_dir)
    finally:
        manager.Destroy()


def _discover_meshes(fbx):
    """Every mesh of one FBX, classified, in scene order."""
    fbx = Path(fbx)
    texture_dir = fbx.with_suffix(".fbm")
    manager, _scene, nodes = fbx_scene.read_scene(fbx)
    try:
        meshes = []
        for node in nodes:
            mesh = fbx_scene.extract_mesh(node, texture_dir)
            mesh["kind"] = classify_mesh(mesh)
            meshes.append(mesh)
        return meshes
    finally:
        manager.Destroy()


def _resolve_base_image(camera, meshes, image_arg):
    """The camera image to draw on: --image, else the camera's base image.

    New models carry a full-frame quad whose texture IS the camera image; the
    legacy 005/006 models have no quad and declare ``base_image_path`` instead.
    """
    if image_arg is not None:
        return read_image(image_arg, "base image")
    if camera.base_image_path is not None:
        return read_image(camera.base_image_path, "base image")
    matches = [mesh for mesh in meshes
               if mesh["kind"] is MeshKind.FULL_FRAME]
    if not matches:
        raise OverlayError(
            f"no full-frame mesh in {camera.fbx} to extract the base image"
        )
    return load_texture(matches[0], "base image")


def _draw(base, meshes, grids, args):
    """`base` with `meshes` outlined in their kind colours, labels unless off."""
    image = draw_meshes(
        base, meshes, colors=[KIND_COLORS[m["kind"]] for m in meshes],
        v_origin=args.uv_v_origin, thickness=args.line_thickness,
        fill_alpha=args.fill_alpha, vertex_radius=args.vertex_radius,
    )
    if args.no_labels:
        return image
    for mesh in meshes:
        image = draw_meter_labels(image, mesh, grids[mesh["node"]],
                                  v_origin=args.uv_v_origin,
                                  color=KIND_COLORS[mesh["kind"]])
    return image


def _render_camera(line, camera, args, out_dir):
    """One sub-camera: mesh.json, the composite, and one image per mesh.

    Products live in ``<out_dir>/<camera>/`` — meshes are named the same across
    cameras (e.g. Plane006 in both), so a flat directory would collide. The
    document's camera field is the LINE name, so a multi-camera line's JSONs
    self-identify.

    mesh.json is written first and independently of --no-labels: it is the
    deliverable, the images are the inspection aid.
    """
    meshes = _discover_meshes(camera.fbx)
    cam_dir = out_dir / camera.name
    document = annotate_document(line, display(camera.fbx), meshes)
    mesh_json = cam_dir / "mesh.json"
    cam_dir.mkdir(parents=True, exist_ok=True)
    mesh_json.write_text(json.dumps(document, indent=2) + "\n",
                         encoding="utf-8")

    drawn = [mesh for mesh in meshes
             if mesh["kind"] is not MeshKind.FULL_FRAME]
    grids = {mesh["node"]: grid_annotation(mesh) for mesh in drawn}
    base = _resolve_base_image(camera, meshes, args.image)

    image = _draw(base, drawn, grids, args)
    composite = cam_dir / f"{camera.name}_mesh_overlay.png"
    write_image(composite, image, "mesh overlay")
    for mesh in drawn:
        write_image(cam_dir / f"{camera.name}_{mesh['node']}_"
                              f"{mesh['kind'].value}_overlay.png",
                    _draw(base, [mesh], grids, args), "mesh overlay")
    for mesh in drawn:
        print(f"{line}/{mesh['node']} ({mesh['kind'].value}): "
              f"{len(mesh['triangles'])} triangles")
    print(f"wrote {mesh_json}")
    print(f"wrote {composite} ({image.shape[1]}x{image.shape[0]})")


def _render_explicit(specs, args, out_dir):
    """The legacy one-mesh-per-FBX regression path (--mesh)."""
    meshes = []
    for path, node_name in specs:
        mesh = _load_mesh(path, node_name)
        mesh["source_fbx"] = str(path)
        mesh["kind"] = classify_mesh(mesh)
        meshes.append(mesh)

    if args.image is not None:
        base = read_image(args.image, "base image")
    else:
        base = load_texture(meshes[0], "base image")

    output = draw_meshes(
        base, meshes,
        colors=[KIND_COLORS.get(mesh["kind"], (0, 255, 255))
                for mesh in meshes],
        v_origin=args.uv_v_origin,
        thickness=args.line_thickness,
        fill_alpha=args.fill_alpha,
        vertex_radius=args.vertex_radius,
    )
    path = out_dir / f"{meshes[0]['node']}_overlay.png"
    write_image(path, output, "mesh overlay")

    for mesh in meshes:
        print(f"{mesh['node']}: {len(mesh['triangles'])} triangles")
    print(f"wrote {path} ({output.shape[1]}x{output.shape[0]})")
    return 1


def _select_lines(args):
    """The lines to render, from --line / --camera / the default (all of them).

    ``--camera`` filters within a line; a camera name with no ``--line`` resolves
    to its owning line (compat shim, so today's ``--camera femto`` keeps working).
    """
    if args.line:
        return list(args.line)
    if args.camera:
        lines = []
        for name in args.camera:
            # A camera name resolves to its owning line (femto -> water_entry2);
            # a bare line name is accepted as-is.
            try:
                line = line_for_camera(name)
            except SystemExit:
                line = name if name in PROFILES else None
            if line is not None and line not in lines:
                lines.append(line)
        return lines
    return profile_names()


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.mesh:
            out_dir = args.output or OUTPUTS / "water_entry" / "overlay"
            _render_explicit(args.mesh, args, out_dir)
            return 0

        wanted = set(args.camera or ())
        lines = _select_lines(args)
        for name in lines:
            profile = get_profile(name)
            # --output overrides; otherwise a single line uses its own dir and
            # multiple lines group under the default outputs root.
            out_dir = (args.output if len(lines) == 1 and args.output
                       else (args.output / name if args.output
                             else profile.out_dir))
            for camera in profile.cameras:
                if not wanted or camera.name in wanted:
                    _render_camera(name, camera, args, out_dir)
        return 0
    except (MediaError, OverlayError, fbx_scene.FbxError) as error:
        print(f"error: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
