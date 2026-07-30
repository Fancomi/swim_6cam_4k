"""Stitch the 16 underwater planes from live .ts video into one panorama mp4.

Reuses the geometry + seam blending already validated for the still stitch
(python.stitch.render): same mesh JSON, same build_remap_clipped, same
seam_weights, same auto bottom-crop. The only difference from render.py is the
texture source — instead of one static image per plane, each plane is driven by
its matching camera clip, and composited frame-by-frame into an h264 mp4 via
ffmpeg (reusing reference_renderer.open_ffmpeg / finish_encoder).

Camera↔plane mapping is positional: extract orders meshes left-to-right by world
X, and the caller passes `camera_ids` in that same order plus a `clip_for`
lookup (python.stitch.profiles.Profile supplies both).

Time alignment: clips must NOT be assumed to share t=0. Each TS starts at its own
decodable keyframe, which the recorder placed somewhere inside the lookback
window — with GOP-sized granularity, so the per-camera skew reaches seconds. The
sample's manifest.json carries the wall-clock truth, and this module follows the
same formula the front-end player uses:

    duration            = (align_end_ms - align_start_ms) / 1000
    offset              in [0, duration]
    source_time_i(offset) = (align_start_ms + offset*1000 - keyframe_ms_i) / 1000

So output frame n (at offset n/fps) reads source frame
`round((align_start_ms - keyframe_ms_i) * fps / 1000) + n` from camera i. File
duration / frame count / size are used only for QC, never as the alignment axis.
"""
import argparse
import json
import shutil
import time
from pathlib import Path

import cv2
import numpy as np

from python.validation import reference_renderer as rr
from python.stitch import render as R
from python.stitch import profiles as P


def load_manifest(video_dir):
    """Read the sample manifest: the common wall-clock window + per-camera anchors.

    Returns (align_start_ms, align_end_ms, fps, {cam: {...}}). Missing manifest or
    missing align window is fatal: without it there is no defensible time axis."""
    path = Path(video_dir) / "manifest.json"
    if not path.is_file():
        raise SystemExit(
            f"manifest.json not found in {video_dir}; cannot time-align "
            "(pass --no-align to stitch from each file's first frame instead)")
    with open(str(path), encoding="utf-8") as f:
        man = json.load(f)
    for key in ("align_start_ms", "align_end_ms"):
        if man.get(key) is None:
            raise SystemExit(f"manifest missing {key}: {path}")
    cams = {}
    for entry in man.get("files", []):
        cam = entry.get("source_id")
        if cam is None:
            continue
        # keyframe_timestamp_ms is the wall clock of this file's frame 0; older
        # manifests only carry first_decodable_timestamp_ms (same semantics).
        anchor = entry.get("keyframe_timestamp_ms")
        if anchor is None:
            anchor = entry.get("first_decodable_timestamp_ms")
        if anchor is None:
            raise SystemExit(f"manifest has no keyframe anchor for {cam}: {path}")
        cams[cam] = {
            "keyframe_ms": anchor,
            "last_decodable_ms": entry.get("last_decodable_timestamp_ms"),
            "frames": entry.get("frames"),
        }
    if not cams:
        raise SystemExit(f"manifest lists no files: {path}")
    return man["align_start_ms"], man["align_end_ms"], man.get("fps"), cams


def alignment_plan(align_start_ms, align_end_ms, fps, cams, order):
    """Per-camera start frame on the common time axis, plus a coverage report.

    `order` is the camera list in mesh order. Returns (starts, report) where
    starts[i] is the source frame index of camera order[i] at offset 0, and
    report[i] describes its coverage of [align_start, align_end]."""
    starts, report = [], []
    for cam in order:
        info = cams.get(cam)
        if info is None:
            raise SystemExit(f"manifest has no entry for {cam}")
        anchor = info["keyframe_ms"]
        skew_ms = align_start_ms - anchor
        start = int(round(skew_ms * fps / 1000.0))
        late = start < 0     # file begins after align_start: no coverage at t=0
        last = info["last_decodable_ms"]
        short_ms = (align_end_ms - last) if last is not None else None
        report.append({
            "cam": cam, "skew_ms": skew_ms, "start_frame": start,
            "late_start": late, "short_ms": short_ms, "frames": info["frames"],
        })
        starts.append(max(0, start))
    return starts, report


def render_video(data_path, video_dir, out_path, camera_ids, clip_for,
                 seconds=None, ppm=None, unit_scale=1.0, neg_v=False,
                 blend_px=0.0, full_res=True, align=True):
    data_path = Path(data_path)
    video_dir = Path(video_dir)
    out_path = Path(out_path)
    if not data_path.is_file():
        raise SystemExit(f"data file does not exist: {data_path}")
    if not video_dir.is_dir():
        raise SystemExit(f"video directory does not exist: {video_dir}")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg is required for video rendering")

    with open(str(data_path), encoding="utf-8") as f:
        loaded = json.load(f)
    if "meshes" not in loaded:
        raise SystemExit(f"data file missing 'meshes' key: {data_path}")
    meshes = loaded["meshes"]

    # Camera identity is positional: extract sorts meshes left-to-right by world
    # X and the profile lists its ids in that same order. Deriving it from the
    # texture filename instead only ever worked for the underA* naming scheme —
    # the overhead textures are 05-02.jpg and C06.jpg.
    cam_order = list(camera_ids)
    if len(cam_order) != len(meshes):
        raise SystemExit(f"camera count mismatch: {len(cam_order)} ids for "
                         f"{len(meshes)} meshes in {data_path}")

    caps, src_wh = [], []
    for cam in cam_order:
        cap = cv2.VideoCapture(str(clip_for(video_dir, cam)))
        if not cap.isOpened():
            raise SystemExit(f"cannot open video for {cam}")
        caps.append(cap)
        src_wh.append((int(cap.get(3)), int(cap.get(4))))

    try:
        rr.to_meters(meshes, unit_scale, neg_v)
        xmin, xmax, ymin, ymax = rr.world_bounds(meshes)
        if ppm is None:
            if full_res:
                src_h = max(h for _w, h in src_wh)
                span_y = ymax - ymin
                ppm = src_h / span_y if span_y > 0 else 100.0
            else:
                ppm = R.resolve_ppm(xmin, xmax, 640)
        # same edge padding as the still renderer so on-edge vertices stay inside
        margin = 2
        pad = margin / ppm
        xmin, ymin = xmin - pad, ymin - pad
        xmax, ymax = xmax + pad, ymax + pad
        out_w = int(round((xmax - xmin) * ppm)) + 1
        out_h = int(round((ymax - ymin) * ppm)) + 1

        layers = [R.build_remap_clipped(m, w, h, xmin, ymin, ppm, out_w, out_h)
                  for m, (w, h) in zip(meshes, src_wh)]
        raw_wts = R.seam_weights([l[2] for l in layers], blend_px)
        wts = [w[..., None] for w in raw_wts]

        # auto bottom-crop rows, computed once from union coverage (static geometry)
        union = np.zeros((out_h, out_w), np.uint8)
        for l in layers:
            union |= l[2]
        crop = R.bottom_dirty_rows(union) if full_res else 0
        target_height = max(h for _w, h in src_wh)
        # width after crop+rescale (kept constant for every frame)
        if crop:
            kept_h = out_h - crop
            final_w = int(round(out_w * target_height / kept_h))
            final_h = target_height
        else:
            final_w, final_h = out_w, out_h
        print(f"canvas {out_w}x{out_h} @ {ppm:.2f}px/m -> output {final_w}x{final_h} "
              f"(bottom crop {crop}px)")

        src_fps = [c.get(cv2.CAP_PROP_FPS) or 30.0 for c in caps]
        n_src = [int(c.get(cv2.CAP_PROP_FRAME_COUNT)) for c in caps]

        if align:
            a_start, a_end, man_fps, cams = load_manifest(video_dir)
            base_fps = man_fps or min(src_fps)
            starts, report = alignment_plan(a_start, a_end, base_fps, cams, cam_order)
            window_s = (a_end - a_start) / 1000.0
            print(f"wall-clock align window {window_s:.3f}s @ {base_fps:g}fps "
                  f"(sync_mode=manifest align_start/align_end)")
            skews = [r["skew_ms"] for r in report]
            print(f"per-camera keyframe skew {min(skews)}..{max(skews)}ms "
                  f"-> start frames {min(starts)}..{max(starts)}")
            for r in report:
                notes = []
                if r["late_start"]:
                    notes.append(f"STARTS {-r['skew_ms']}ms AFTER align_start "
                                 "(no coverage at t=0)")
                if r["short_ms"] is not None and r["short_ms"] > 1000.0 / base_fps:
                    notes.append(f"ends {r['short_ms']}ms before align_end")
                if notes:
                    print(f"  QC {r['cam']}: {'; '.join(notes)}")
            n_window = int(round(window_s * base_fps))
        else:
            base_fps = min(src_fps)
            starts = [0] * len(caps)
            n_window = None
            print(f"NO time alignment: reading every clip from frame 0 "
                  f"(base {base_fps:.2f}fps)")

        steps = [f / base_fps for f in src_fps]
        # bounded by each clip's remaining frames after its aligned start
        avail = [int((n - s) / st) for n, s, st in zip(n_src, starts, steps)]
        max_out = max(0, min(avail))
        n_out = max_out if n_window is None else min(max_out, n_window)
        if seconds is not None:
            n_out = min(n_out, int(seconds * base_fps))
        if n_out <= 0:
            raise SystemExit("no overlapping frames across cameras")
        print(f"{n_out} output frames (~{n_out / base_fps:.1f}s)")

        out_path.parent.mkdir(parents=True, exist_ok=True)
        enc = rr.open_ffmpeg(ffmpeg, out_path, final_w, final_h, base_fps)

        cur = [None] * len(caps)
        src_pos = [-1] * len(caps)
        # seek each clip to its aligned start by decoding forward (TS keyframe
        # indexes are unreliable for random access, and the offsets are small)
        for i, c in enumerate(caps):
            for _ in range(starts[i] + 1):
                ok, fr = c.read()
                if not ok:
                    raise SystemExit(
                        f"{cam_order[i]}: ran out of frames while seeking to "
                        f"aligned start frame {starts[i]}")
                cur[i] = fr
            src_pos[i] = starts[i]

        t0 = time.perf_counter()
        n = 0
        write_error = None
        encoder_finished = False
        try:
            try:
                while n < n_out:
                    ok_all = True
                    for i, c in enumerate(caps):
                        target = starts[i] + int(round(n * steps[i]))
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
                    comp = rr.composite(layers, wts, cur, out_h, out_w)
                    if crop:
                        comp = R.crop_bottom_and_scale(comp, crop, target_height)
                    enc.stdin.write(comp.tobytes())
                    n += 1
                    if n % 100 == 0:
                        el = time.perf_counter() - t0
                        print(f"  {n}/{n_out}  {n / el:.1f} fps")
            except BrokenPipeError as exc:
                write_error = exc

            return_code, close_error = rr.finish_encoder(enc)
            encoder_finished = True
            if return_code != 0:
                raise SystemExit(
                    f"ffmpeg failed for video {out_path}: exit code {return_code}")
            pipe_error = write_error or close_error
            if pipe_error is not None:
                raise SystemExit(f"ffmpeg pipe failed for {out_path}: {pipe_error}")
            el = time.perf_counter() - t0
            print(f"wrote video {out_path}: {n} frames in {el:.1f}s -> {n / el:.1f} fps")
        finally:
            if enc is not None and not encoder_finished:
                enc.kill()
                enc.wait()
        return final_w, final_h, n
    finally:
        for c in caps:
            c.release()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Stitch plane textures from video")
    ap.add_argument("video_dir", type=Path,
                    help="directory holding one clip per camera")
    ap.add_argument("--profile", default="underwater",
                    help="stitch line whose camera ids and clip suffix to use "
                         "(default: %(default)s)")
    ap.add_argument("--data", type=Path, default=None,
                    help="mesh JSON (default: the profile's)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output mp4 (default: <profile out_dir>/stitch.mp4)")
    ap.add_argument("--seconds", type=float, default=None,
                    help="cap output duration; default uses the whole align window")
    ap.add_argument("--ppm", type=float, default=None,
                    help="pixels per metre; default adapts to source height in --full-res")
    ap.add_argument("--unit-scale", type=float, default=1.0)
    ap.add_argument("--neg-v", dest="neg_v", action="store_true", default=False)
    ap.add_argument("--blend-px", type=float, default=None,
                    help="horizontal pixels blended across each vertical seam "
                         "(default: the profile's)")
    ap.add_argument("--no-full-res", action="store_true",
                    help="skip source-height rescale / bottom auto-crop")
    ap.add_argument("--no-align", action="store_true",
                    help="ignore manifest wall clocks and read every clip from "
                         "frame 0; already implied for profiles whose "
                         "recordings carry no wall clock")
    args = ap.parse_args(argv)

    profile = P.get(args.profile)
    # A profile whose recordings have no usable wall clock (sync="none") reads
    # from frame 0 anyway; --no-align forces that for a manifest-bearing one.
    align = profile.sync == "manifest" and not args.no_align
    render_video(
        args.data or profile.mesh_json,
        args.video_dir,
        args.out or profile.out_dir / "stitch.mp4",
        camera_ids=profile.camera_ids,
        clip_for=profile.clip_for,
        seconds=args.seconds,
        ppm=args.ppm if args.ppm is not None else profile.ppm,
        unit_scale=args.unit_scale,
        neg_v=args.neg_v,
        blend_px=args.blend_px if args.blend_px is not None else profile.blend_px,
        full_res=profile.full_res and not args.no_full_res,
        align=align,
    )


if __name__ == "__main__":
    main()
