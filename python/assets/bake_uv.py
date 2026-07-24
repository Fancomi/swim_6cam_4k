"""Bake the centre-line UV extension directly into the FBX.

Replicates render_center_ext.extend_centerline_uv, but writes the modified UVs
back into the FBX so downstream tools need NO special processing — just read the
baked FBX normally. For every polygon-vertex whose control point sits on the pool
centre line (world Z ~ CENTER_Z), we push its UV V toward the image top by
ext_px/texH (capped at 1.0).

UV write is done by appending a new entry to the direct array and repointing the
index (eIndexToDirect) or setting in place (eDirect), so shared UVs are never
clobbered for non-centre vertices.
"""
import argparse
import os
from pathlib import Path

import fbx
from . import fbx_common

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

CENTER_Z = 14.736   # pool centre in world meters (kept axis Z); seam of the two banks
EPS = 0.02


def node_full_matrix(node):
    gx = node.EvaluateGlobalTransform()
    gt = node.GetGeometricTranslation(fbx.FbxNode.EPivotSet.eSourcePivot)
    gr = node.GetGeometricRotation(fbx.FbxNode.EPivotSet.eSourcePivot)
    gs = node.GetGeometricScaling(fbx.FbxNode.EPivotSet.eSourcePivot)
    geom = fbx.FbxAMatrix()
    geom.SetTRS(gt, gr, gs)
    return gx * geom


def texture_uvset_name(node):
    if node.GetMaterialCount() == 0:
        return None
    mat = node.GetMaterial(0)
    prop = mat.FindProperty(fbx.FbxSurfaceMaterial.sDiffuse)
    if not prop.IsValid():
        return None
    crit = fbx.FbxCriteria.ObjectType(fbx.FbxFileTexture.ClassId)
    if prop.GetSrcObjectCount(crit) == 0:
        return None
    return str(prop.GetSrcObject(crit, 0).UVSet.Get())


def pick_uv_element(mesh, uvset_name):
    n = mesh.GetElementUVCount()
    if n == 0:
        return None
    if uvset_name:
        for i in range(n):
            if mesh.GetElementUV(i).GetName() == uvset_name:
                return mesh.GetElementUV(i)
    return mesh.GetElementUV(0)


def bake_mesh(node, ext_px, tex_dir):
    mesh = node.GetMesh()
    full = node_full_matrix(node)
    uvset = texture_uvset_name(node)
    uv = pick_uv_element(mesh, uvset)
    if uv is None:
        return 0

    # dV uses the composite texture height the UVs were authored against
    name = os.path.basename(str(node.GetMaterial(0).FindProperty(
        fbx.FbxSurfaceMaterial.sDiffuse).GetSrcObject(
        fbx.FbxCriteria.ObjectType(fbx.FbxFileTexture.ClassId), 0).GetFileName()))
    import cv2
    texture_path = tex_dir / name
    texture = cv2.imread(str(texture_path), cv2.IMREAD_GRAYSCALE)
    if texture is None:
        raise SystemExit(f"cannot read texture: {texture_path}")
    th = texture.shape[0]
    dv = ext_px / th

    mode = uv.GetMappingMode()
    ref = uv.GetReferenceMode()
    da = uv.GetDirectArray()
    ia = uv.GetIndexArray()

    # world Z per control point -> which are on the centre line
    ncp = mesh.GetControlPointsCount()
    on_center = [False] * ncp
    for i in range(ncp):
        w = full.MultT(mesh.GetControlPointAt(i))
        on_center[i] = abs(w[2] - CENTER_Z) < EPS

    bumped = 0
    pv = 0
    for p in range(mesh.GetPolygonCount()):
        for j in range(mesh.GetPolygonSize(p)):
            cp = mesh.GetPolygonVertex(p, j)
            if on_center[cp]:
                if mode == fbx.FbxLayerElement.EMappingMode.eByControlPoint:
                    base = cp
                else:
                    base = pv
                if ref == fbx.FbxLayerElement.EReferenceMode.eIndexToDirect:
                    di = ia.GetAt(base)
                    t = da.GetAt(di)
                    newv = min(1.0, t[1] + dv)
                    ni = da.Add(fbx.FbxVector2(t[0], newv))  # fresh entry, no sharing
                    ia.SetAt(base, ni)
                else:  # eDirect: one entry per pv, safe to set in place
                    t = da.GetAt(base)
                    da.SetAt(base, fbx.FbxVector2(t[0], min(1.0, t[1] + dv)))
                bumped += 1
            pv += 1
    print(f"  {node.GetName():9s} {name[:8]} th={th} dv={dv:.4f} bumped {bumped} polygon-verts")
    return bumped


def walk(node, ext_px, tex_dir):
    for i in range(node.GetChildCount()):
        c = node.GetChild(i)
        attr = c.GetNodeAttribute()
        if attr and attr.GetAttributeType() == fbx.FbxNodeAttribute.EType.eMesh:
            bake_mesh(c, ext_px, tex_dir)
        walk(c, ext_px, tex_dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("dst", type=Path)
    ap.add_argument("--ext-px", type=float, default=5.0)
    ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "pool" / "textures")
    args = ap.parse_args()

    src = args.src
    dst = args.dst
    tex_dir = args.tex_dir
    if not src.is_file():
        raise SystemExit(f"source file does not exist: {src}")
    if not tex_dir.is_dir():
        raise SystemExit(f"texture directory does not exist: {tex_dir}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    mgr, scene = fbx_common.InitializeSdkObjects()
    if not fbx_common.LoadScene(mgr, scene, str(src)):
        raise SystemExit(f"failed to load {src}")
    print(f"baking centre-line UV +{args.ext_px}px into {src}:")
    walk(scene.GetRootNode(), args.ext_px, tex_dir)
    if not fbx_common.SaveScene(mgr, scene, str(dst)):
        raise SystemExit(f"failed to save {dst}")
    print(f"wrote {dst}")
    mgr.Destroy()


if __name__ == "__main__":
    main()
