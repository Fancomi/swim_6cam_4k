"""Stitch one clip per camera into a single panorama mp4.

Same geometry and seam blending as the still (python.stitch.compose); the only
difference is that each lane is driven by a clip instead of a static image.

Time alignment matters and cannot be assumed. Each recorded .ts starts at its own
decodable keyframe, placed anywhere inside the recorder's lookback window with
GOP granularity, so the per-camera skew reaches seconds. Samples that carry a
manifest are aligned by the same formula the front-end player uses:

    duration              = (align_end_ms - align_start_ms) / 1000
    source_time_i(offset) = (align_start_ms + offset*1000 - keyframe_ms_i) / 1000

so output frame n reads source frame `round((align_start - keyframe_i) * fps /
1000) + n` from camera i. File duration, frame count and size are QC only, never
the alignment axis. Lines whose recordings have no manifest (sync="none") read
every clip from frame 0.
"""
import json
import time
from pathlib import Path

import cv2

from python.align.mesh import warp_meshes
from python.common.media import close_encoder, open_encoder
from python.stitch import compose as C
from python.stitch.profiles import StepError


def load_manifest(video_dir):
    """(align_start_ms, align_end_ms, fps, {camera: anchors}) for a sample.

    A missing manifest or a missing align window is fatal: without it there is no
    defensible time axis, and silently reading from frame 0 would look correct
    while putting the lanes seconds apart."""
    path = Path(video_dir) / "manifest.json"
    if not path.is_file():
        raise StepError(
            f"manifest.json not found in {video_dir}; cannot time-align "
            "(pass --no-align to read every clip from its first frame)")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    for key in ("align_start_ms", "align_end_ms"):
        if manifest.get(key) is None:
            raise StepError(f"manifest missing {key}: {path}")
    cameras = {}
    for entry in manifest.get("files", []):
        camera = entry.get("source_id")
        if camera is None:
            continue
        # keyframe_timestamp_ms is the wall clock of this file's frame 0; older
        # manifests spell the same thing first_decodable_timestamp_ms.
        anchor = (entry.get("keyframe_timestamp_ms")
                  or entry.get("first_decodable_timestamp_ms"))
        if anchor is None:
            raise StepError(f"manifest has no keyframe anchor for {camera}: {path}")
        cameras[camera] = {
            "keyframe_ms": anchor,
            "last_decodable_ms": entry.get("last_decodable_timestamp_ms"),
            "frames": entry.get("frames"),
        }
    if not cameras:
        raise StepError(f"manifest lists no files: {path}")
    return (manifest["align_start_ms"], manifest["align_end_ms"],
            manifest.get("fps"), cameras)


def alignment_plan(align_start_ms, align_end_ms, fps, cameras, order):
    """Per-camera start frame on the common axis, plus a coverage report."""
    starts, report = [], []
    for camera in order:
        info = cameras.get(camera)
        if info is None:
            raise StepError(f"manifest has no entry for {camera}")
        skew_ms = align_start_ms - info["keyframe_ms"]
        start = int(round(skew_ms * fps / 1000.0))
        last = info["last_decodable_ms"]
        report.append({
            "cam": camera, "skew_ms": skew_ms, "start_frame": start,
            # Negative skew means the file begins after align_start: that lane has
            # no coverage at t=0 and its start clamps to zero.
            "late_start": start < 0,
            "short_ms": (align_end_ms - last) if last is not None else None,
            "frames": info["frames"],
        })
        starts.append(max(0, start))
    return starts, report


def lane_offsets_ms(profile, video_dir):
    """Milliseconds into each clip where the common axis starts.

    Shared with the realtime path so the offline mp4 and the GPU stitch align by
    exactly the same arithmetic. A line with sync="none" returns {} without
    looking; a manifest-bearing one that has no manifest says so and degrades to
    the same empty result rather than failing a whole run over it."""
    if profile.sync != "manifest":
        return {}
    try:
        align_start, align_end, fps, cameras = load_manifest(video_dir)
    except StepError as error:
        print(f"  no wall-clock alignment: {error}")
        return {}
    order = [c for c in profile.camera_ids if c in cameras]
    _starts, report = alignment_plan(align_start, align_end, fps, cameras, order)
    offsets = {entry["cam"]: max(0, entry["skew_ms"]) for entry in report}
    skews = [entry["skew_ms"] for entry in report]
    print(f"  wall-clock align window {(align_end - align_start) / 1000:.3f}s; "
          f"lane skew {min(skews)}..{max(skews)}ms")
    for entry in report:
        if entry["late_start"]:
            print(f"  QC {entry['cam']}: starts {-entry['skew_ms']}ms after "
                  "align_start (no coverage at t=0)")
    return offsets


def loop_period_ms(profile, video_dir, offsets):
    """Shortest usable span across lanes, in ms — the common content period.

    Each lane can play from its aligned start to its own last decodable frame, and
    those spans differ by tens of milliseconds. Restarting each lane at its own end
    would let them drift apart on every pass; wrapping every lane on the shortest
    span keeps them locked together indefinitely. 0 means "no manifest", which
    tells the runtime to use each file's natural end."""
    if profile.sync != "manifest":
        return 0
    try:
        *_unused, cameras = load_manifest(video_dir)
    except StepError:
        return 0
    spans = []
    for camera, info in cameras.items():
        last, anchor = info.get("last_decodable_ms"), info.get("keyframe_ms")
        if last is None or anchor is None:
            continue
        spans.append(last - anchor - offsets.get(camera, 0))
    return max(0, min(spans)) if spans else 0


def render(profile, video_dir, out_path, seconds=None, ppm=None, blend_px=None,
           full_res=None, align=True, alignments=None):
    """Composite every lane's clip into one mp4. Returns (width, height, frames).

    `align` is TIME alignment (which frame of each clip is t=0); `alignments` is
    drift correction on the UVs (python/align). Two different problems that both
    ended up called alignment — the first is about when, the second about where.
    """
    video_dir = Path(video_dir)
    if not video_dir.is_dir():
        raise StepError(f"video directory does not exist: {video_dir}")

    meshes = C.load_meshes(profile.mesh_json, neg_v=profile.neg_v,
                          neg_u=profile.neg_u)
    meshes = warp_meshes(meshes, alignments)
    cameras = list(profile.camera_ids)
    if len(cameras) != len(meshes):
        raise StepError(f"{len(cameras)} cameras for {len(meshes)} meshes")

    captures, sizes = [], []
    for camera in cameras:
        capture = cv2.VideoCapture(str(profile.clip_for(video_dir, camera)))
        if not capture.isOpened():
            raise StepError(f"cannot open the clip for {camera}")
        captures.append(capture)
        sizes.append((int(capture.get(3)), int(capture.get(4))))

    try:
        full_res = profile.full_res if full_res is None else full_res
        if ppm is None:
            ppm = (C.adaptive_ppm(meshes, max(h for _w, h in sizes))
                   if full_res else profile.ppm)
        blend_px = profile.blend_px if blend_px is None else blend_px

        canvas = C.Canvas(meshes, ppm, margin=profile.still_margin)
        layers = [C.build_remap(mesh, canvas, size, clip=profile.clip_uv)
                  for mesh, size in zip(meshes, sizes)]
        weights = [w[..., None] for w in
                   C.blend_weights([layer[2] for layer in layers], blend_px)]

        # Geometry is static, so the crop is measured once and every frame gets
        # the same one; a per-frame measurement would make the output size wobble.
        crop = (C.bottom_dirty_rows(C.union_coverage(layers, canvas))
                if full_res else 0)
        target_height = max(h for _w, h in sizes)
        if crop:
            final_h = target_height
            final_w = int(round(canvas.width * target_height / (canvas.height - crop)))
        else:
            final_w, final_h = canvas.width, canvas.height
        print(f"canvas {canvas.width}x{canvas.height} @ {ppm:.2f}px/m "
              f"-> output {final_w}x{final_h} (bottom crop {crop}px)")

        source_fps = [capture.get(cv2.CAP_PROP_FPS) or 30.0 for capture in captures]
        source_frames = [int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
                         for capture in captures]

        window_frames = None
        if align and profile.sync == "manifest":
            start_ms, end_ms, manifest_fps, entries = load_manifest(video_dir)
            base_fps = manifest_fps or min(source_fps)
            starts, report = alignment_plan(start_ms, end_ms, base_fps,
                                            entries, cameras)
            window_s = (end_ms - start_ms) / 1000.0
            skews = [entry["skew_ms"] for entry in report]
            print(f"wall-clock align window {window_s:.3f}s @ {base_fps:g}fps; "
                  f"keyframe skew {min(skews)}..{max(skews)}ms "
                  f"-> start frames {min(starts)}..{max(starts)}")
            for entry in report:
                notes = []
                if entry["late_start"]:
                    notes.append(f"starts {-entry['skew_ms']}ms after align_start "
                                 "(no coverage at t=0)")
                if (entry["short_ms"] is not None
                        and entry["short_ms"] > 1000.0 / base_fps):
                    notes.append(f"ends {entry['short_ms']}ms before align_end")
                if notes:
                    print(f"  QC {entry['cam']}: {'; '.join(notes)}")
            window_frames = int(round(window_s * base_fps))
        else:
            base_fps = min(source_fps)
            starts = [0] * len(captures)
            print(f"no time alignment: every clip read from frame 0 "
                  f"(base {base_fps:.2f}fps)")

        # Higher-fps sources are subsampled to the lowest source fps by nearest
        # frame. This aligns the frame RATE only, never the capture start time.
        steps = [fps / base_fps for fps in source_fps]
        available = [int((count - start) / step)
                     for count, start, step in zip(source_frames, starts, steps)]
        frames = max(0, min(available))
        if window_frames is not None:
            frames = min(frames, window_frames)
        if seconds is not None:
            frames = min(frames, int(seconds * base_fps))
        if frames <= 0:
            raise StepError("no overlapping frames across cameras")
        print(f"{frames} output frames (~{frames / base_fps:.1f}s)")

        encoder = open_encoder(out_path, final_w, final_h, base_fps)
        current = [None] * len(captures)
        position = list(starts)
        # Decode forward to each aligned start: .ts keyframe indexes are
        # unreliable for random access and the offsets are small.
        for index, capture in enumerate(captures):
            for _ in range(starts[index] + 1):
                ok, frame = capture.read()
                if not ok:
                    raise StepError(
                        f"{cameras[index]}: ran out of frames while seeking to "
                        f"aligned start frame {starts[index]}")
                current[index] = frame

        started = time.perf_counter()
        written = 0
        finished = False
        try:
            while written < frames:
                complete = True
                for index, capture in enumerate(captures):
                    target = starts[index] + int(round(written * steps[index]))
                    while position[index] < target:
                        ok, frame = capture.read()
                        if not ok:
                            complete = False
                            break
                        current[index] = frame
                        position[index] += 1
                    if not complete:
                        break
                if not complete:
                    break
                composite = C.composite(layers, weights, current, canvas)
                if crop:
                    composite = C.crop_and_scale(composite, crop, target_height)
                encoder.stdin.write(composite.tobytes())
                written += 1
                if written % 100 == 0:
                    elapsed = time.perf_counter() - started
                    print(f"  {written}/{frames}  {written / elapsed:.1f} fps")
            # Inside the try so a BrokenPipeError raised while draining is
            # reported as one, and outside the loop so a short clip still
            # finalises the container.
            close_encoder(encoder, out_path)
            finished = True
        except BrokenPipeError as error:
            raise StepError(f"ffmpeg pipe failed for {out_path}: {error}") from None
        finally:
            if not finished:
                encoder.kill()
                encoder.wait()
        elapsed = time.perf_counter() - started
        print(f"wrote {out_path}: {written} frames in {elapsed:.1f}s "
              f"-> {written / elapsed:.1f} fps")
        return final_w, final_h, written
    finally:
        for capture in captures:
            capture.release()
