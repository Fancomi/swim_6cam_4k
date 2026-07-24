"""Extract underwater FBX into pool-compatible mesh JSON, ordered left-to-right.

Reuses python.assets.extract_fbx for all FBX/UV/geometry logic; this module only
adds underwater-specific defaults, left-to-right ordering, correct texture
selection for multi-material meshes, and an isolated CLI.
"""
import argparse
import json
import os
from collections import Counter
from pathlib import Path

import fbx
from python.assets import extract_fbx, fbx_common

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"


def _mesh_min_x(mesh):
    xs = [v["pos"][0] for tri in mesh["triangles"] for v in tri]
    return min(xs) if xs else float("inf")


def sort_meshes_by_world_x(meshes):
    """Return meshes ordered by each mesh's minimum world-X (pos[0]) ascending.

    Empty meshes (no triangles) sort last. Input list is not mutated."""
    return sorted(meshes, key=_mesh_min_x)


def _mesh_span(mesh, axis):
    vs = [v["pos"][axis] for tri in mesh["triangles"] for v in tri]
    return (min(vs), max(vs)) if vs else (float("inf"), float("-inf"))


def select_pool_planes(meshes, band=(-11.6, -8.0), min_height=2.5):
    """Keep exactly one full-height pool plane per texture.

    A clean file (e.g. 1-5.fbx) has one plane per texture; all.fbx carries the
    16 real planes plus clutter — untextured frames, and per texture a set of
    lane-marker strips sitting near Y≈0 plus alternate copies. The real swimming
    plane is the tall mesh (height > `min_height`) whose vertical (world-Y,
    pos[1]) extent falls inside the pool `band`. Among candidates sharing a
    texture, keep the one with the most triangles. Meshes without a texture are
    dropped. Returns the survivors (input not mutated)."""
    lo, hi = band
    best = {}
    for m in meshes:
        tex = m["texture_basename"]
        if not tex:
            continue
        y0, y1 = _mesh_span(m, 1)
        if y0 < lo or y1 > hi or (y1 - y0) <= min_height:
            continue
        cur = best.get(tex)
        if cur is None or len(m["triangles"]) > len(cur["triangles"]):
            best[tex] = m
    return list(best.values())


def used_material_index(node):
    """Index of the material actually painted on this mesh's polygons.

    extract_fbx always reads material 0, which is wrong when a newer modelling
    strategy attaches many materials and selects one per polygon (eByPolygon).
    For eAllSame (or no material element) the answer is 0, matching extract_fbx."""
    mesh = node.GetMesh()
    elem = mesh.GetElementMaterial() if mesh else None
    if elem is None:
        return 0
    if elem.GetMappingMode() == fbx.FbxLayerElement.EMappingMode.eByPolygon:
        ia = elem.GetIndexArray()
        if ia.GetCount() == 0:
            return 0
        return Counter(ia.GetAt(k) for k in range(ia.GetCount())).most_common(1)[0][0]
    return 0


def texture_for_material(node, idx, tex_dir):
    """(uvset, basename, display_path) of the diffuse FileTexture on material idx."""
    if idx >= node.GetMaterialCount():
        return None, None, None
    prop = node.GetMaterial(idx).FindProperty(fbx.FbxSurfaceMaterial.sDiffuse)
    if not prop.IsValid():
        return None, None, None
    crit = fbx.FbxCriteria.ObjectType(fbx.FbxFileTexture.ClassId)
    if prop.GetSrcObjectCount(crit) == 0:
        return None, None, None
    tex = prop.GetSrcObject(crit, 0)
    basename = os.path.basename(str(tex.GetFileName()))
    return str(tex.UVSet.Get()), basename, extract_fbx.display_path(Path(tex_dir) / basename)


def _mesh_nodes(node, out):
    """Collect mesh-attribute nodes depth-first (nodes, not extracted dicts)."""
    for i in range(node.GetChildCount()):
        c = node.GetChild(i)
        attr = c.GetNodeAttribute()
        if attr and attr.GetAttributeType() == fbx.FbxNodeAttribute.EType.eMesh:
            out.append(c)
        _mesh_nodes(c, out)


def extract_to_json(src, dst, tex_dir, planes_only=False):
    src = Path(src)
    dst = Path(dst)
    tex_dir = Path(tex_dir)
    if not src.is_file():
        raise SystemExit(f"source file does not exist: {src}")
    if not tex_dir.is_dir():
        raise SystemExit(f"texture directory does not exist: {tex_dir}")

    mgr, scene = fbx_common.InitializeSdkObjects()
    if not fbx_common.LoadScene(mgr, scene, str(src)):
        raise SystemExit(f"FAILED to load {src}")
    nodes = []
    _mesh_nodes(scene.GetRootNode(), nodes)
    meshes = []
    for node in nodes:
        m = extract_fbx.extract_mesh(node, tex_dir)
        # correct texture selection for multi-material (eByPolygon) meshes
        idx = used_material_index(node)
        if idx != 0:
            uvset, basename, disp = texture_for_material(node, idx, tex_dir)
            if basename is not None:
                m["uvset"], m["texture_basename"], m["texture"] = uvset, basename, disp
        meshes.append(m)
    mgr.Destroy()

    if not meshes:
        raise SystemExit(f"no mesh found in {src}")
    if planes_only:
        # all.fbx-style scenes carry clutter (untextured frames, lane-marker
        # strips, duplicate plane copies) alongside the real swimming planes;
        # keep one full-height plane per texture.
        meshes = select_pool_planes(meshes)
        if not meshes:
            raise SystemExit(f"no pool plane found in {src}")
    meshes = sort_meshes_by_world_x(meshes)

    data = {"source": extract_fbx.display_path(src), "meshes": meshes}
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(str(dst), "w", encoding="utf-8") as f:
        json.dump(data, f)
    for m in meshes:
        print(f"{m['node']:12s} tris={len(m['triangles']):4d} "
              f"tex={m['texture_basename']} uvset={m['uvset']}")
    print("wrote", dst)
    return meshes


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract underwater FBX to mesh JSON")
    ap.add_argument("src", nargs="?", type=Path, default=INPUTS_DIR / "underwater" / "models" / "01d.fbx")
    ap.add_argument("dst", nargs="?", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_mesh.json")
    ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "underwater" / "models" / "01d.fbm")
    ap.add_argument("--planes-only", action="store_true",
                    help="keep only full-height pool planes (one per texture); "
                         "use for all.fbx-style scenes that carry frames, "
                         "lane-marker strips, and duplicate meshes")
    args = ap.parse_args(argv)
    extract_to_json(args.src, args.dst, args.tex_dir, planes_only=args.planes_only)


if __name__ == "__main__":
    main()
