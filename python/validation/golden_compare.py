"""Golden-still harness: export the six composite textures as raw RGBA for the
GPU backends, render the Python reference still, and compare a backend's raw
BGRA/ RGBA readback against that reference per pixel (PSNR + max/mean diff).

Usage:
  # 1. export inputs + reference (once)
  python -m python.validation.golden_compare export --out benchmarks/golden

  # 2. after a backend writes benchmarks/golden/<name>.raw (RGBA8, encoded size)
  python -m python.validation.golden_compare compare \
      --out benchmarks/golden --backend d3d11
"""

import argparse
import json
import struct
from pathlib import Path

import cv2
import numpy as np

from python.validation.reference_renderer import (
    build_remap,
    composite,
    feather_weights,
    to_meters,
    world_bounds,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"

CAMERA_ORDER = ("cam3", "cam2", "cam1", "cam4", "cam5", "cam6")


def _load_raw(path, width, height, channels):
    data = np.fromfile(str(path), dtype=np.uint8)
    expected = width * height * channels
    if data.size < expected:
        raise SystemExit(
            f"{path}: {data.size} bytes < expected {expected} "
            f"({width}x{height}x{channels})")
    return data[:expected].reshape(height, width, channels)


def _reference_from_video_frame0(mesh_path, videos):
    """Render the Python reference composite from frame 0 of each source clip,
    using the exact video-path remap+feather+composite. Returns BGR uint8 at the
    logical 5001x2101 canvas."""
    with open(str(mesh_path), encoding="utf-8") as f:
        meshes = json.load(f)["meshes"]
    to_meters(meshes, 1.0, True)
    xmin, xmax, ymin, ymax = world_bounds(meshes)
    out_w = int(round((xmax - xmin) * 100.0)) + 1
    out_h = int(round((ymax - ymin) * 100.0)) + 1

    frames = []
    for v in videos:
        cap = cv2.VideoCapture(str(v))
        ok, frame = cap.read()
        cap.release()
        if not ok:
            raise SystemExit(f"cannot read frame 0 from {v}")
        frames.append(frame)

    layers = [build_remap(m, fr.shape[1], fr.shape[0], xmin, ymin, 100.0,
                          out_w, out_h)
              for m, fr in zip(meshes, frames)]
    wts = [w[..., None] for w in feather_weights([l[2] for l in layers])]
    comp = composite(layers, wts, [f.astype(np.float32) for f in frames],
                     out_h, out_w)
    return comp  # BGR, (2101, 5001, 3)


def _to_bgr(image, fmt):
    """Normalize a backend readback to BGR uint8, cropping the 5002x2102 encoded
    canvas to the 5001x2101 logical content region (right col + bottom row are
    padding)."""
    if fmt == "bgra":
        bgr = image[:, :, :3]
    elif fmt == "rgba":
        bgr = image[:, :, [2, 1, 0]]
    else:
        raise SystemExit(f"unknown format {fmt}")
    return np.ascontiguousarray(bgr[:2101, :5001, :])


def _metrics(a, b, label):
    a = a.astype(np.float32)
    b = b.astype(np.float32)
    diff = np.abs(a - b)
    mse = float(np.mean(diff * diff))
    psnr = float("inf") if mse == 0 else 10.0 * np.log10(255.0 * 255.0 / mse)
    print(f"[{label}] max_diff={diff.max():.1f} mean_diff={diff.mean():.3f} "
          f"PSNR={psnr:.2f} dB")
    return psnr, diff


def cmd_export(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    videos = [Path(args.dataset) / f"20260629_172532_{c}.mp4" for c in CAMERA_ORDER]
    ref = _reference_from_video_frame0(args.mesh, videos)
    cv2.imwrite(str(out / "reference.png"), ref)
    np.ascontiguousarray(ref).tofile(str(out / "reference.bgr"))
    print(f"wrote reference {ref.shape} -> {out/'reference.png'}")


def cmd_compare(args):
    out = Path(args.out)
    ref = _load_raw(out / "reference.bgr", 5001, 2101, 3)
    backend = _load_raw(args.raw, args.width, args.height, 4)
    backend_bgr = _to_bgr(backend, args.format)
    psnr, diff = _metrics(ref, backend_bgr, args.backend)
    if args.diff_png:
        cv2.imwrite(args.diff_png,
                    np.clip(diff * args.diff_gain, 0, 255).astype(np.uint8))
        print(f"wrote diff image {args.diff_png}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="render python reference from video frame 0")
    e.add_argument("--out", default="benchmarks/golden")
    e.add_argument("--mesh", type=Path,
                   default=OUTPUTS_DIR / "data" / "pool_mesh.json")
    e.add_argument("--dataset",
                   default="D:/WindowsProject/workspace/SWIM/20260629-4K")
    e.set_defaults(func=cmd_export)

    c = sub.add_parser("compare", help="compare a backend raw readback to reference")
    c.add_argument("--out", default="benchmarks/golden")
    c.add_argument("--raw", type=Path, required=True)
    c.add_argument("--backend", default="backend")
    c.add_argument("--format", choices=("bgra", "rgba"), required=True)
    c.add_argument("--width", type=int, default=5002)
    c.add_argument("--height", type=int, default=2102)
    c.add_argument("--diff-png", default=None)
    c.add_argument("--diff-gain", type=float, default=4.0)
    c.set_defaults(func=cmd_compare)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

