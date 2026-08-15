"""Read one line's FBX meshes and draw their triangles over the camera image.

Lines mirror the stitch chain's model — a rebuilt FBX of the same cameras is a
new line with a ``2`` suffix (water_entry -> water_entry2, overhead ->
overhead2). Four exist::

    .venv/bin/python -m python.fbx_overlay                  # water_entry + water_entry2
    .venv/bin/python -m python.fbx_overlay --line water_entry2
    .venv/bin/python -m python.fbx_overlay --line water_entry2 --camera gemini
    .venv/bin/python -m python.fbx_overlay --camera femto   # compat: maps to water_entry2
    .venv/bin/python -m python.fbx_overlay --line overhead2 --texture-set dataset

Base-image lines (water_entry / water_entry2): each sub-camera's FBX contains
every mesh for that camera — the full-frame quad whose texture IS the camera
image, plus the vertical water-plane and water-surface meshes. The base image
is extracted from the FBX, so no separate camera image argument is needed.

Canvas lines (overhead / overhead2): the two planes are stitched into one
world-projected canvas, and the overlay + meters are drawn on that canvas
against a lane-schematic reference (``label_line.png``).

The meshes are classified automatically (see classify.py) and drawn over the
base image in per-kind colors. Grid lines carry real-world meters (see
meters.py) — written per line to ``mesh.json`` with the full geometry, and
drawn as meter labels on the overlays (``--no-labels`` turns the text off, the
JSON is still written). Use ``--mesh FBX NODE`` to draw one explicit mesh from
any FBX (regression path). Output goes to a directory, one composite image per
camera plus one image per mesh.
"""
import argparse
import json
from pathlib import Path

from python.common.media import MediaError, read_image, write_image
from python.common.paths import OUTPUTS, display
from python.fbx_tools import scene as fbx_scene

from .classify import MeshKind, classify_mesh
from .meters import annotate_document, grid_annotation, pool_rightmost
from .overlay_stitch import (build_canvas, draw_canvas_overlay,
                             draw_label_line_compare, resolve_texture_set)
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
        help="line to render; may be repeated (default: base-image lines)",
    )
    parser.add_argument(
        "--camera", action="append",
        help="sub-camera to render within a line; may be repeated "
             "(e.g. femto, gemini, water_entry_a), or a line name for the "
             "compat shim (e.g. overhead)",
    )
    parser.add_argument(
        "--image", type=Path, default=None,
        help="override the base image for every camera "
             "(default: the camera image extracted from the FBX)",
    )
    parser.add_argument(
        "--output", type=Path, default=None,
        help="output directory (default: outputs/<line>)",
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
    parser.add_argument(
        "--texture-set", default=None,
        help="overhead texture set: 'fbx' (embedded) or 'dataset' (default: "
             "the profile's first set)",
    )
    parser.add_argument(
        "--label-line", type=Path, default=None,
        help="overhead reference image to compare the overlay against "
             "(default: the profile's label_line)",
    )
    parser.add_argument(
        "--no-label-line", action="store_true",
        help="skip the label_line comparison image",
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


def _camera_paths(out_dir, camera, meshes):
    """(composite, per-mesh paths, mesh.json) for one camera's products.

    Products live in ``<out_dir>/<camera>/`` — meshes are named the same
    across cameras (e.g. Plane006 in both), so a flat directory would collide.
    """
    cam_dir = out_dir / camera.name
    composite = cam_dir / f"{camera.name}_mesh_overlay.png"
    per_mesh = {}
    for mesh in meshes:
        if mesh["kind"] is MeshKind.FULL_FRAME:
            continue        # the base image; no separate product
        per_mesh[mesh["node"]] = cam_dir / (
            f"{camera.name}_{mesh['node']}_{mesh['kind'].value}_overlay.png"
        )
    return composite, per_mesh, cam_dir / "mesh.json"


def _render_camera(line, camera, args, out_dir):
    """Render all of one sub-camera's products; returns the number of images."""
    meshes = _discover_meshes(camera.fbx)
    base = _resolve_base_image(camera, meshes, args.image)
    composite, per_mesh, mesh_json = _camera_paths(out_dir, camera, meshes)

    drawn = [mesh for mesh in meshes
             if mesh["kind"] is not MeshKind.FULL_FRAME]
    grids = {mesh["node"]: grid_annotation(mesh) for mesh in drawn}

    # The meters annotation is a deliverable on its own, independent of whether
    # the labels are drawn — always write it. The camera field is the LINE name
    # so a multi-camera line's JSONs self-identify.
    document = annotate_document(line, display(camera.fbx), drawn)
    mesh_json.parent.mkdir(parents=True, exist_ok=True)
    mesh_json.write_text(json.dumps(document, indent=2) + "\n",
                         encoding="utf-8")

    colors = [KIND_COLORS[mesh["kind"]] for mesh in drawn]
    composite_image = draw_meshes(
        base, drawn, colors=colors,
        v_origin=args.uv_v_origin,
        thickness=args.line_thickness,
        fill_alpha=args.fill_alpha,
        vertex_radius=args.vertex_radius,
    )
    if not args.no_labels:
        for mesh in drawn:
            composite_image = draw_meter_labels(
                composite_image, mesh, grids[mesh["node"]],
                v_origin=args.uv_v_origin,
                color=KIND_COLORS[mesh["kind"]])
    write_image(composite, composite_image, "mesh overlay")

    for mesh in drawn:
        path = per_mesh[mesh["node"]]
        single = draw_meshes(
            base, [mesh], colors=[KIND_COLORS[mesh["kind"]]],
            v_origin=args.uv_v_origin,
            thickness=args.line_thickness,
            fill_alpha=args.fill_alpha,
            vertex_radius=args.vertex_radius,
        )
        if not args.no_labels:
            single = draw_meter_labels(
                single, mesh, grids[mesh["node"]],
                v_origin=args.uv_v_origin,
                color=KIND_COLORS[mesh["kind"]])
        write_image(path, single, "mesh overlay")

    for mesh in drawn:
        print(f"{camera.name}/{mesh['node']} ({mesh['kind'].value}): "
              f"{len(mesh['triangles'])} triangles")
    print(f"wrote {composite} ({composite_image.shape[1]}x"
          f"{composite_image.shape[0]})")
    print(f"wrote {mesh_json}")
    return 1 + len(per_mesh)


def _render_overhead_canvas(profile, args, out_dir):
    """Stitch the overhead planes into a canvas and overlay the grid + meters.

    Products land in ``<out_dir>/`` (default ``outputs/<line>/``, e.g.
    ``outputs/overhead2/``):
    ``<line>_mesh_overlay_<set>.png`` (composite + edges + labels),
    per-plane ``<line>_<node>_plane_overlay_<set>.png``, and a side-by-side
    comparison against ``label_line.png``. The mesh.json (geometry + meters) is
    texture-set independent, so it is written once.
    """
    if args.image is not None:
        raise OverlayError(
            "--image is not valid for the overhead canvas mode; the composite "
            "is built from the texture set instead")

    meshes = _discover_meshes(profile.fbx)
    tex_dir, tex_names, set_name = resolve_texture_set(profile,
                                                       args.texture_set)
    canvas, _layers, _weights, composite = build_canvas(
        profile, meshes, tex_dir, tex_names)

    rightmost = pool_rightmost(meshes)
    grids = {mesh["node"]: grid_annotation(mesh, rightmost_x=rightmost)
             for mesh in meshes}
    document = annotate_document(profile.name, display(profile.fbx), meshes,
                                 rightmost)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "mesh.json").write_text(
        json.dumps(document, indent=2) + "\n", encoding="utf-8")

    overlay = composite.copy()
    if not args.no_labels:
        overlay = draw_canvas_overlay(overlay, meshes, canvas, grids)
    composite_path = out_dir / f"{profile.name}_mesh_overlay_{set_name}.png"
    write_image(composite_path, overlay, "mesh overlay")

    for mesh in meshes:
        single = composite.copy()
        if not args.no_labels:
            single = draw_canvas_overlay(single, [mesh], canvas,
                                         {mesh["node"]: grids[mesh["node"]]})
        path = out_dir / (f"{profile.name}_{mesh['node']}_plane_overlay_"
                          f"{set_name}.png")
        write_image(path, single, "mesh overlay")

    if not args.no_label_line and profile.label_line is not None:
        label_path = args.label_line or profile.label_line
        draw_label_line_compare(
            overlay, label_path,
            out_dir / f"{profile.name}_label_line_compare_{set_name}.png")

    for mesh in meshes:
        print(f"{profile.name}/{mesh['node']} ({mesh['kind'].value}): "
              f"{len(mesh['triangles'])} triangles")
    print(f"wrote {composite_path} "
          f"({overlay.shape[1]}x{overlay.shape[0]}) "
          f"[{set_name}]")
    print(f"wrote {out_dir / 'mesh.json'}")
    return 1 + len(meshes)


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
    """The lines to render, from --line / --camera / the default.

    ``--line`` selects lines directly. ``--camera`` filters within a line; a
    camera name with no ``--line`` resolves to its owning line (compat shim, so
    today's ``--camera femto`` keeps working). Nothing given renders the
    base-image lines (water_entry + water_entry2).
    """
    if args.line:
        return list(args.line)
    if args.camera:
        lines = []
        for name in args.camera:
            # A camera name resolves to its owning line (femto -> water_entry2);
            # a bare line name (overhead) is accepted as-is.
            try:
                line = line_for_camera(name)
            except SystemExit:
                line = name if name in PROFILES else None
            if line is not None and line not in lines:
                lines.append(line)
        return lines
    return [name for name in profile_names()
            if PROFILES[name].mode != "canvas"]


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.mesh:
            out_dir = args.output or OUTPUTS / "water_entry"
            _render_explicit(args.mesh, args, out_dir)
            return 0

        wanted = set(args.camera or ())
        lines = _select_lines(args)
        count = 0
        for name in lines:
            profile = get_profile(name)
            # --output overrides; otherwise a single line uses its own dir and
            # multiple lines group under the default outputs root.
            out_dir = (args.output if len(lines) == 1 and args.output
                       else (args.output / name if args.output
                             else profile.out_dir))
            if profile.mode == "canvas":
                count += _render_overhead_canvas(profile, args, out_dir)
            else:
                cameras = [camera for camera in profile.cameras
                           if not wanted or camera.name in wanted]
                for camera in cameras:
                    count += _render_camera(name, camera, args, out_dir)
        return 0
    except (MediaError, OverlayError, fbx_scene.FbxError) as error:
        print(f"error: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
