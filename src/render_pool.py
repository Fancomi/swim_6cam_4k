"""Render the pool stitch from a baked-UV mesh json — still (from composite
textures) and/or video (from the source clips). No special processing: all UVs,
including the centre-line extension, are baked into the FBX this json came from.

Geometry is constant across the clip, so each mesh's per-triangle affine is
precomputed once as a (mapx, mapy) remap table; every frame is then cv2.remap +
a feathered composite over overlaps. The two camera banks meet with no overlap
at the pool centre line, giving a clean hard seam for free.

Videos pair with meshes by order (the order extract_fbx.py prints). Sources with
a higher fps are subsampled to the lowest source fps (nearest frame); this aligns
frame RATE only, not capture start time.

  # still from composite textures
  python render_pool.py --data mesh.json --still still.png
  # video from source clips, h264
  python render_pool.py --data mesh.json --videos c3 c2 c1 c4 c5 c6 \
      --video pool_30s.mp4 --seconds 30
"""
import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"


def to_meters(meshes, unit_scale, neg_v):
    for m in meshes:
        for tri in m["triangles"]:
            for v in tri:
                v["pos"][0] /= unit_scale
                v["pos"][1] = (-v["pos"][1] if neg_v else v["pos"][1]) / unit_scale


def world_bounds(meshes):
    xs = [v["pos"][0] for m in meshes for t in m["triangles"] for v in t]
    ys = [v["pos"][1] for m in meshes for t in m["triangles"] for v in t]
    return min(xs), max(xs), min(ys), max(ys)


def build_remap(mesh, tex_w, tex_h, xmin, ymin, ppm, out_w, out_h):
    """Precompute the inverse map (output px -> source px) + coverage mask."""
    mapx = np.zeros((out_h, out_w), np.float32)
    mapy = np.zeros((out_h, out_w), np.float32)
    mask = np.zeros((out_h, out_w), np.uint8)
    for tri in mesh["triangles"]:
        dst = np.array([[(v["pos"][0] - xmin) * ppm,
                         out_h - 1 - (v["pos"][1] - ymin) * ppm] for v in tri], np.float32)
        src = np.array([[v["uv"][0] * tex_w,
                         (1.0 - v["uv"][1]) * tex_h] for v in tri], np.float32)
        x, y, w, h = cv2.boundingRect(dst)
        if w <= 0 or h <= 0:
            continue
        dloc = (dst - np.float32([x, y])).astype(np.float32)
        M = cv2.getAffineTransform(dloc, src)  # local output -> source
        gy, gx = np.mgrid[0:h, 0:w]
        gx = gx.astype(np.float32)
        gy = gy.astype(np.float32)
        sx = M[0, 0] * gx + M[0, 1] * gy + M[0, 2]
        sy = M[1, 0] * gx + M[1, 1] * gy + M[1, 2]
        mloc = np.zeros((h, w), np.uint8)
        cv2.fillConvexPoly(mloc, np.int32(dloc), 1)
        mm = mloc > 0
        mapx[y:y + h, x:x + w][mm] = sx[mm]
        mapy[y:y + h, x:x + w][mm] = sy[mm]
        mask[y:y + h, x:x + w][mm] = 1
    m1, m2 = cv2.convertMaps(mapx, mapy, cv2.CV_16SC2)
    return m1, m2, mask.astype(np.uint8)


def feather_weights(masks):
    """Per-layer blend weights from distance-to-own-boundary, normalized per pixel.

    Single-coverage pixels get weight 1 (no edge darkening); overlaps blend
    smoothly; disjoint regions (the centre seam) stay a clean hard cut."""
    dists = []
    for mk in masks:
        d = cv2.distanceTransform(
            cv2.copyMakeBorder(mk, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=1),
            cv2.DIST_L2, 3)[1:-1, 1:-1]
        dists.append(d * (mk > 0))
    total = np.maximum(sum(dists), 1e-6)
    return [(d / total).astype(np.float32) for d in dists]


# distinct BGR colours, one per mesh/camera region (up to 6)
GRID_COLORS = [
    (60, 60, 220),    # red
    (60, 200, 60),    # green
    (220, 120, 40),   # blue
    (40, 200, 220),   # yellow
    (200, 200, 40),   # cyan
    (200, 60, 200),   # magenta
]


def draw_grid(img, meshes, xmin, ymin, ppm, out_h):
    """Overlay per-mesh triangle edges (thin) + region outline (thick), one
    colour per mesh. Geometry is in world metres already; project to canvas px
    the same way build_remap does so the grid lines up with the composite."""
    for idx, mesh in enumerate(meshes):
        color = GRID_COLORS[idx % len(GRID_COLORS)]
        region = np.zeros((out_h, img.shape[1]), np.uint8)
        for tri in mesh["triangles"]:
            dst = np.array([[(v["pos"][0] - xmin) * ppm,
                             out_h - 1 - (v["pos"][1] - ymin) * ppm] for v in tri], np.int32)
            cv2.polylines(img, [dst], True, color, 1, cv2.LINE_AA)
            cv2.fillConvexPoly(region, dst, 1)
        # region outline: contour of the union of this mesh's triangles
        cnts, _ = cv2.findContours(region, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(img, cnts, -1, color, 3, cv2.LINE_AA)
    return img


def write_image(path, image, kind):
    try:
        ok = cv2.imwrite(str(path), image)
    except cv2.error as exc:
        raise SystemExit(f"cannot write {kind} image: {path}: {exc}") from None
    if not ok:
        raise SystemExit(f"cannot write {kind} image: {path}")


def open_ffmpeg(ffmpeg, out_path, w, h, fps):
    """ffmpeg reading raw bgr24 frames from stdin, encoding h264 (yuv420p)."""
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{w}x{h}", "-r", f"{fps:.4f}", "-i", "-",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",  # yuv420p needs even dims
        "-pix_fmt", "yuv420p", str(out_path),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


def finish_encoder(enc):
    pipe_error = None
    if enc.stdin is not None and not enc.stdin.closed:
        try:
            enc.stdin.close()
        except OSError as exc:
            pipe_error = exc
    return enc.wait(), pipe_error


def composite(layers, weights, frames, out_h, out_w):
    acc = np.zeros((out_h, out_w, 3), np.float32)
    for (m1, m2, _mask), fr, wt in zip(layers, frames, weights):
        w = cv2.remap(fr, m1, m2, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101)
        acc += w.astype(np.float32) * wt
    return np.clip(acc, 0, 255).astype(np.uint8)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=OUTPUTS_DIR / "data" / "pool_mesh.json")
    ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "textures")
    ap.add_argument("--ppm", type=float, default=100.0)
    ap.add_argument("--unit-scale", type=float, default=1.0)
    ap.add_argument("--no-neg-v", dest="neg_v", action="store_false", default=True)
    ap.add_argument("--still", type=Path, default=None,
                    help="render a still PNG from composite textures")
    ap.add_argument("--grid-still", type=Path, default=None,
                    help="render a still PNG with per-mesh triangle grid + region outline overlaid")
    ap.add_argument("--videos", nargs="+", type=Path, default=None,
                    help="source clips in mesh order; required for --video")
    ap.add_argument("--video", type=Path, default=None,
                    help="render an h264 mp4 from --videos")
    ap.add_argument("--seconds", type=float, default=30.0)
    args = ap.parse_args()

    if not args.data.is_file():
        raise SystemExit(f"data file does not exist: {args.data}")
    with open(str(args.data), encoding="utf-8") as f:
        meshes = json.load(f)["meshes"]
    to_meters(meshes, args.unit_scale, args.neg_v)
    xmin, xmax, ymin, ymax = world_bounds(meshes)
    out_w = int(round((xmax - xmin) * args.ppm)) + 1
    out_h = int(round((ymax - ymin) * args.ppm)) + 1
    print(f"canvas {out_w}x{out_h} @ {args.ppm}px/m")

    if args.still or args.grid_still:
        texs = []
        for m in meshes:
            path = args.tex_dir / m["texture_basename"]
            texture = cv2.imread(str(path))
            if texture is None:
                raise SystemExit(f"cannot read texture: {path}")
            texs.append(texture)
        layers = [build_remap(m, t.shape[1], t.shape[0], xmin, ymin, args.ppm, out_w, out_h)
                  for m, t in zip(meshes, texs)]
        wts = [w[..., None] for w in feather_weights([l[2] for l in layers])]
        comp = composite(layers, wts, [t.astype(np.float32) for t in texs], out_h, out_w)
        if args.still:
            args.still.parent.mkdir(parents=True, exist_ok=True)
            write_image(args.still, comp, "still")
            print(f"wrote still {args.still}")
        if args.grid_still:
            grid = draw_grid(comp.copy(), meshes, xmin, ymin, args.ppm, out_h)
            args.grid_still.parent.mkdir(parents=True, exist_ok=True)
            write_image(args.grid_still, grid, "grid")
            print(f"wrote grid still {args.grid_still}")

    if args.video:
        if not args.videos or len(args.videos) != len(meshes):
            raise SystemExit(f"--video needs {len(meshes)} --videos in mesh order")
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise SystemExit("ffmpeg is required for video rendering")
        for v in args.videos:
            if not v.is_file():
                raise SystemExit(f"source video does not exist: {v}")
        args.video.parent.mkdir(parents=True, exist_ok=True)
        caps = []
        enc = None
        encoder_finished = False
        try:
            for v in args.videos:
                c = cv2.VideoCapture(str(v))
                caps.append(c)
                if not c.isOpened():
                    raise SystemExit(f"cannot open {v}")
            src_fps = [c.get(cv2.CAP_PROP_FPS) or 30.0 for c in caps]
            base_fps = min(src_fps)
            steps = [f / base_fps for f in src_fps]
            print(f"src fps {[round(f,2) for f in src_fps]} -> base {base_fps:.2f}, "
                  f"steps {[round(s,2) for s in steps]}")

            layers = [build_remap(m, int(c.get(3)), int(c.get(4)), xmin, ymin,
                                  args.ppm, out_w, out_h)
                      for m, c in zip(meshes, caps)]
            wts = [w[..., None] for w in feather_weights([l[2] for l in layers])]

            n_out = int(args.seconds * base_fps)
            enc = open_ffmpeg(ffmpeg, args.video, out_w, out_h, base_fps)
            cur = [None] * len(caps)
            src_pos = [-1] * len(caps)
            for i, c in enumerate(caps):
                ok, cur[i] = c.read()
                if not ok:
                    raise SystemExit(f"empty video index {i}")
                src_pos[i] = 0

            t0 = time.perf_counter()
            n = 0
            write_error = None
            try:
                while n < n_out:
                    ok_all = True
                    for i, c in enumerate(caps):
                        target = int(round(n * steps[i]))
                        while src_pos[i] < target:
                            ok, fr = c.read()
                            if not ok:
                                ok_all = False
                                break
                            cur[i] = fr
                            src_pos[i] += 1
                        if not ok_all:
                            break
                    if not ok_all:
                        break
                    enc.stdin.write(composite(layers, wts, cur, out_h, out_w).tobytes())
                    n += 1
                    if n % 100 == 0:
                        el = time.perf_counter() - t0
                        print(f"  {n}/{n_out}  {n/el:.1f} fps")
            except BrokenPipeError as exc:
                write_error = exc

            return_code, close_error = finish_encoder(enc)
            encoder_finished = True
            if return_code != 0:
                raise SystemExit(
                    f"ffmpeg failed for video {args.video}: exit code {return_code}")
            pipe_error = write_error or close_error
            if pipe_error is not None:
                raise SystemExit(f"ffmpeg pipe failed for video {args.video}: {pipe_error}")

            el = time.perf_counter() - t0
            print(f"wrote video {args.video}: {n} frames in {el:.1f}s -> {n/el:.1f} fps")
        finally:
            if enc is not None and not encoder_finished:
                finish_encoder(enc)
            for c in caps:
                c.release()


if __name__ == "__main__":
    main()
