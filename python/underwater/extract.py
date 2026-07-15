"""Extract 01d-style FBX into pool-compatible mesh JSON, ordered left-to-right.

Reuses python.assets.extract_fbx for all FBX/UV/geometry logic; this module only
adds underwater-specific defaults, left-to-right ordering, and an isolated CLI.
"""
import argparse
import json
from pathlib import Path

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


def extract_to_json(src, dst, tex_dir):
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
    meshes = []
    extract_fbx.walk(scene.GetRootNode(), meshes, tex_dir)
    mgr.Destroy()

    if not meshes:
        raise SystemExit(f"no mesh found in {src}")
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
    ap.add_argument("src", nargs="?", type=Path, default=INPUTS_DIR / "models" / "01d.fbx")
    ap.add_argument("dst", nargs="?", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_mesh.json")
    ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "models" / "01d.fbm")
    args = ap.parse_args(argv)
    extract_to_json(args.src, args.dst, args.tex_dir)


if __name__ == "__main__":
    main()
