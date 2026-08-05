"""Read FBX meshes and draw their UV triangles over a camera image.

Example::

    .venv/bin/python -m python.fbx_overlay \
        --output outputs/water_entry/fbx_mesh_overlay.png

The default image is ``inputs/water_entry/background.jpg``. Use
``--image`` to override it. Use ``--mesh FBX NODE`` repeatedly to override the
two default models. The
default V origin is ``bottom`` because that is the FBX convention; use
``--uv-v-origin top`` when the asset was authored in image coordinates.
"""
import argparse
from pathlib import Path

from python.common.media import MediaError, read_image, write_image
from python.common.paths import INPUTS
from python.fbx_tools import scene as fbx_scene

from .render import OverlayError, draw_meshes


DEFAULT_IMAGE = INPUTS / "water_entry/background.jpg"
DEFAULT_MESHES = (
    (INPUTS / "water_entry/models/006.fbx", "Plane004"),
    (INPUTS / "water_entry/models/005.fbx", "Plane005"),
)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image", type=Path, default=DEFAULT_IMAGE,
        help=f"xlj camera image corresponding to the UVs (default: {DEFAULT_IMAGE})",
    )
    parser.add_argument("--output", required=True, type=Path,
                        help="output image path")
    parser.add_argument(
        "--mesh", action="append", nargs=2, metavar=("FBX", "NODE"),
        type=str, help="FBX path and exact mesh node name; may be repeated",
    )
    parser.add_argument("--uv-v-origin", choices=("bottom", "top"),
                        default="bottom")
    parser.add_argument("--line-thickness", type=int, default=2)
    parser.add_argument("--fill-alpha", type=float, default=0.0)
    parser.add_argument("--vertex-radius", type=int, default=0)
    return parser


def _load_mesh(path, node_name):
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


def _mesh_specs(values):
    if values is None:
        return DEFAULT_MESHES
    return tuple((Path(path), node) for path, node in values)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        image = read_image(args.image, "base image")
        specs = _mesh_specs(args.mesh)
        meshes = []
        for path, node_name in specs:
            mesh = _load_mesh(path, node_name)
            mesh["source_fbx"] = str(path)
            meshes.append(mesh)
        output = draw_meshes(
            image,
            meshes,
            v_origin=args.uv_v_origin,
            thickness=args.line_thickness,
            fill_alpha=args.fill_alpha,
            vertex_radius=args.vertex_radius,
        )
        write_image(args.output, output, "mesh overlay")
    except (MediaError, OverlayError, fbx_scene.FbxError) as error:
        print(f"error: {error}")
        return 2

    for mesh in meshes:
        print(f"{mesh['node']}: {len(mesh['triangles'])} triangles")
    print(f"wrote {args.output} ({output.shape[1]}x{output.shape[0]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
