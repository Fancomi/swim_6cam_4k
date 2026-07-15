"""Render the underwater stitch from a mesh JSON: still + grid diagnostic.

Reuses python.validation.reference_renderer for all remap/feather/composite/grid
logic; this module only adds underwater defaults, width-adaptive ppm, and an
isolated CLI that writes into outputs/underwater/.
"""
import argparse
import json
from pathlib import Path

import cv2

from python.validation import reference_renderer as rr

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"


def resolve_ppm(xmin, xmax, target_width):
    """Pixels-per-metre so world-X span maps to ~target_width px. Fallback 100.0."""
    span = xmax - xmin
    if span <= 0:
        return 100.0
    return target_width / span


def render_stills(data_path, tex_dir, still_path, grid_path,
                  ppm=None, unit_scale=1.0, neg_v=False, target_width=640):
    data_path = Path(data_path)
    tex_dir = Path(tex_dir)
    if not data_path.is_file():
        raise SystemExit(f"data file does not exist: {data_path}")
    with open(str(data_path), encoding="utf-8") as f:
        loaded = json.load(f)
    if "meshes" not in loaded:
        raise SystemExit(f"data file missing 'meshes' key: {data_path}")
    meshes = loaded["meshes"]

    rr.to_meters(meshes, unit_scale, neg_v)
    xmin, xmax, ymin, ymax = rr.world_bounds(meshes)
    if ppm is None:
        ppm = resolve_ppm(xmin, xmax, target_width)
    out_w = int(round((xmax - xmin) * ppm)) + 1
    out_h = int(round((ymax - ymin) * ppm)) + 1
    print(f"canvas {out_w}x{out_h} @ {ppm:.2f}px/m")

    texs = []
    for m in meshes:
        path = tex_dir / m["texture_basename"]
        texture = cv2.imread(str(path))
        if texture is None:
            raise SystemExit(f"cannot read texture: {path}")
        texs.append(texture)
    layers = [rr.build_remap(m, t.shape[1], t.shape[0], xmin, ymin, ppm, out_w, out_h)
              for m, t in zip(meshes, texs)]
    wts = [w[..., None] for w in rr.feather_weights([l[2] for l in layers])]
    comp = rr.composite(layers, wts, [t.astype("float32") for t in texs], out_h, out_w)

    if still_path is not None:
        still_path = Path(still_path)
        still_path.parent.mkdir(parents=True, exist_ok=True)
        rr.write_image(still_path, comp, "still")
        print(f"wrote still {still_path}")
    if grid_path is not None:
        grid_path = Path(grid_path)
        grid = rr.draw_grid(comp.copy(), meshes, xmin, ymin, ppm, out_h)
        grid_path.parent.mkdir(parents=True, exist_ok=True)
        rr.write_image(grid_path, grid, "grid")
        print(f"wrote grid still {grid_path}")
    return out_w, out_h


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render underwater stitch still + grid")
    ap.add_argument("--data", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_mesh.json")
    ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "models" / "01d.fbm")
    ap.add_argument("--still", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_stitch.png")
    ap.add_argument("--grid-still", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_grid.png")
    ap.add_argument("--ppm", type=float, default=None,
                    help="pixels per metre; default adapts world-X span to --target-width")
    ap.add_argument("--target-width", type=int, default=640)
    ap.add_argument("--unit-scale", type=float, default=1.0)
    ap.add_argument("--neg-v", dest="neg_v", action="store_true", default=False,
                    help="flip Y (world V) when compositing; off by default (upright for 01d)")
    args = ap.parse_args(argv)
    render_stills(args.data, args.tex_dir, args.still, args.grid_still,
                  ppm=args.ppm, unit_scale=args.unit_scale, neg_v=args.neg_v,
                  target_width=args.target_width)


if __name__ == "__main__":
    main()
