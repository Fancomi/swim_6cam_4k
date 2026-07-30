"""Bake the centre-line UV extension into a new FBX.

The pool's two banks meet along the centre line, and the composite textures stop
a few pixels short of it — so the stitch shows a thin seam there. Pushing the V of
every centre-line vertex toward the image top by ext_px/texture_height makes each
bank sample a few rows further in, closing the gap.

Baked into the model rather than applied at render time, so every downstream tool
reads a normal FBX with no special case. The write appends a fresh direct-array
entry and repoints the index (eIndexToDirect) rather than editing in place, so a
UV shared with a non-centre vertex is never clobbered.

    python -m python.fbx_tools.bake_uv SRC.fbx DST.fbx --ext-px 5
"""
import argparse
from pathlib import Path

import fbx

from python.common.media import read_image
from python.common.paths import INPUTS
from python.fbx_tools import scene as F

CENTRE_Z = 14.736    # pool centre in world metres (the kept Z axis)
EPS = 0.02


def bake_mesh(node, ext_px, tex_dir):
    """Push the V of this mesh's centre-line vertices; returns how many moved."""
    mesh = node.GetMesh()
    uvset, basename = F.diffuse_texture(node)
    element = F.uv_element(mesh, uvset)
    if element is None or basename is None:
        return 0

    # dV is in units of the texture the UVs were authored against, so it must be
    # that image's height and not the canvas or a nominal 1080.
    height = read_image(tex_dir / basename, "texture").shape[0]
    dv = ext_px / height

    matrix = F.node_matrix(node)
    on_centre = [abs(matrix.MultT(mesh.GetControlPointAt(index))[2] - CENTRE_Z) < EPS
                 for index in range(mesh.GetControlPointsCount())]

    by_control_point = (element.GetMappingMode()
                        == fbx.FbxLayerElement.EMappingMode.eByControlPoint)
    indexed = (element.GetReferenceMode()
               == fbx.FbxLayerElement.EReferenceMode.eIndexToDirect)
    direct, indices = element.GetDirectArray(), element.GetIndexArray()

    moved, polygon_vertex = 0, 0
    for polygon in range(mesh.GetPolygonCount()):
        for corner in range(mesh.GetPolygonSize(polygon)):
            control_point = mesh.GetPolygonVertex(polygon, corner)
            if on_centre[control_point]:
                slot = control_point if by_control_point else polygon_vertex
                if indexed:
                    value = direct.GetAt(indices.GetAt(slot))
                    fresh = direct.Add(fbx.FbxVector2(value[0],
                                                      min(1.0, value[1] + dv)))
                    indices.SetAt(slot, fresh)
                else:
                    value = direct.GetAt(slot)
                    direct.SetAt(slot, fbx.FbxVector2(value[0],
                                                      min(1.0, value[1] + dv)))
                moved += 1
            polygon_vertex += 1
    print(f"  {node.GetName():10s} {basename[:16]:16s} h={height} dv={dv:.4f} "
          f"moved {moved} polygon-vertices")
    return moved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("src", type=Path)
    parser.add_argument("dst", type=Path)
    parser.add_argument("--ext-px", type=float, default=5.0,
                        help="pixels to extend the centre-line UV (default: %(default)s)")
    parser.add_argument("--tex-dir", type=Path, default=INPUTS / "pool" / "textures")
    args = parser.parse_args(argv)

    if not args.tex_dir.is_dir():
        raise SystemExit(f"texture directory does not exist: {args.tex_dir}")
    args.dst.parent.mkdir(parents=True, exist_ok=True)

    manager, scene, nodes = F.read_scene(args.src)
    try:
        print(f"baking centre-line UV +{args.ext_px}px into {args.src}:")
        for node in nodes:
            bake_mesh(node, args.ext_px, args.tex_dir)
        if not F.save_scene(manager, scene, args.dst):
            raise SystemExit(f"failed to save {args.dst}")
    finally:
        manager.Destroy()
    print(f"wrote {args.dst}")


if __name__ == "__main__":
    main()
