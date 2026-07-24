"""Extract warp data from the pool FBX into a plain JSON the renderer can use.

For each mesh node we emit triangles with, per vertex:
  - world position projected onto the two varying axes (the constant axis is dropped)
  - UV (normalized source-image coords) from the UV set the diffuse texture uses

Heavy fbx dependency lives ONLY here. Output is consumed by render_pool.py.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import fbx
from . import fbx_common

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"


def display_path(path):
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def detect_constant_axis(pts):
    """pts: list of (x,y,z). Return index of axis with ~zero span + the two kept axes."""
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    for v in pts:
        for k in range(3):
            lo[k] = min(lo[k], v[k])
            hi[k] = max(hi[k], v[k])
    spans = [hi[k] - lo[k] for k in range(3)]
    const = min(range(3), key=lambda k: spans[k])
    kept = [k for k in range(3) if k != const]
    return const, kept, spans


def texture_uvset_name(node):
    """Name of the UV set referenced by the diffuse FileTexture, or None."""
    if node.GetMaterialCount() == 0:
        return None, None
    mat = node.GetMaterial(0)
    prop = mat.FindProperty(fbx.FbxSurfaceMaterial.sDiffuse)
    if not prop.IsValid():
        return None, None
    crit = fbx.FbxCriteria.ObjectType(fbx.FbxFileTexture.ClassId)
    if prop.GetSrcObjectCount(crit) == 0:
        return None, None
    tex = prop.GetSrcObject(crit, 0)
    return str(tex.UVSet.Get()), str(tex.GetFileName())


def pick_uv_element(mesh, uvset_name):
    """Choose the FbxLayerElementUV matching uvset_name, else the first."""
    n = mesh.GetElementUVCount()
    if n == 0:
        return None
    if uvset_name:
        for i in range(n):
            if mesh.GetElementUV(i).GetName() == uvset_name:
                return mesh.GetElementUV(i)
    return mesh.GetElementUV(0)


def uv_at(uv_elem, poly_vertex_index, control_point_index):
    """Read a UV given mapping/reference mode. Handles the common combos."""
    mode = uv_elem.GetMappingMode()
    ref = uv_elem.GetReferenceMode()
    da = uv_elem.GetDirectArray()
    ia = uv_elem.GetIndexArray()
    if mode == fbx.FbxLayerElement.EMappingMode.eByControlPoint:
        idx = control_point_index
    else:  # eByPolygonVertex
        idx = poly_vertex_index
    if ref == fbx.FbxLayerElement.EReferenceMode.eIndexToDirect:
        idx = ia.GetAt(idx)
    t = da.GetAt(idx)
    return [t[0], t[1]]


def extract_mesh(node, tex_dir):
    mesh = node.GetMesh()
    gx = node.EvaluateGlobalTransform()  # node transform (does NOT include geometric xform)

    # geometric transform: applied to the mesh only, not propagated to children, and
    # NOT part of EvaluateGlobalTransform. Maya bakes it into what you see, so we must
    # apply it too or meshes land in the wrong place. world = gx * geom * controlPoint
    gt = node.GetGeometricTranslation(fbx.FbxNode.EPivotSet.eSourcePivot)
    gr = node.GetGeometricRotation(fbx.FbxNode.EPivotSet.eSourcePivot)
    gs = node.GetGeometricScaling(fbx.FbxNode.EPivotSet.eSourcePivot)
    geom = fbx.FbxAMatrix()
    geom.SetTRS(gt, gr, gs)
    full = gx * geom

    ncp = mesh.GetControlPointsCount()
    world_pts = []
    for i in range(ncp):
        wp = full.MultT(mesh.GetControlPointAt(i))
        world_pts.append((wp[0], wp[1], wp[2]))

    const, kept, spans = detect_constant_axis(world_pts)
    uvset, texfile = texture_uvset_name(node)
    uv_elem = pick_uv_element(mesh, uvset)

    tris = []
    pv = 0  # running polygon-vertex counter
    for p in range(mesh.GetPolygonCount()):
        size = mesh.GetPolygonSize(p)
        verts = []
        for j in range(size):
            cp = mesh.GetPolygonVertex(p, j)
            w = world_pts[cp]
            pos = [w[kept[0]], w[kept[1]]]
            uv = uv_at(uv_elem, pv, cp) if uv_elem else [0.0, 0.0]
            verts.append({"pos": pos, "uv": uv})
            pv += 1
        # fan-triangulate (all are tris here, but be safe)
        for j in range(1, size - 1):
            tris.append([verts[0], verts[j], verts[j + 1]])

    texture_basename = os.path.basename(texfile) if texfile else None
    return {
        "node": node.GetName(),
        "texture": display_path(tex_dir / texture_basename) if texture_basename else None,
        "texture_basename": texture_basename,
        "uvset": uvset,
        "const_axis": const,
        "kept_axes": kept,  # which world axes pos[0],pos[1] correspond to
        "spans": spans,
        "triangles": tris,
    }


def walk(node, out, tex_dir):
    for i in range(node.GetChildCount()):
        c = node.GetChild(i)
        attr = c.GetNodeAttribute()
        if attr and attr.GetAttributeType() == fbx.FbxNodeAttribute.EType.eMesh:
            out.append(extract_mesh(c, tex_dir))
        walk(c, out, tex_dir)


def main(src, dst, tex_dir):
    if not src.is_file():
        raise SystemExit(f"source file does not exist: {src}")
    if not tex_dir.is_dir():
        raise SystemExit(f"texture directory does not exist: {tex_dir}")
    mgr, scene = fbx_common.InitializeSdkObjects()
    if not fbx_common.LoadScene(mgr, scene, str(src)):
        print("FAILED to load", src)
        sys.exit(1)
    meshes = []
    walk(scene.GetRootNode(), meshes, tex_dir)
    mgr.Destroy()
    data = {"source": display_path(src), "meshes": meshes}
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(str(dst), "w", encoding="utf-8") as f:
        json.dump(data, f)
    for m in meshes:
        print(f"{m['node']:10s} tris={len(m['triangles']):4d} "
              f"const_axis={m['const_axis']} kept={m['kept_axes']} "
              f"tex={m['texture_basename']} uvset={m['uvset']}")
    print("wrote", dst)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src", nargs="?", type=Path, default=INPUTS_DIR / "pool" / "models" / "pool.fbx")
    ap.add_argument("dst", nargs="?", type=Path, default=OUTPUTS_DIR / "data" / "pool_mesh.json")
    ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "pool" / "textures")
    args = ap.parse_args()
    main(args.src, args.dst, args.tex_dir)
