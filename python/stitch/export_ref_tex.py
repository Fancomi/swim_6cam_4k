"""Export one reference texture per camera: the frame the stitch really sees.

The textures a designer bakes into the .fbm carry calibration overlays (yellow
lane lines, distance labels), which is what you want when checking geometry and
not what you want when judging image quality. This writes each camera's first
captured frame instead, so `render --real` stitches photographic imagery by
swapping one directory.

Files are named `<camera_id>.png`, not after the mesh's texture_basename:
- the overhead basenames (05-02.jpg, C06.jpg) name a designer's working file,
  so a directory full of them says nothing about which camera is which;
- reusing a `.jpg` basename would re-encode a lossless decode as JPEG. Measured
  on cam5: max channel error 35, and the stitched result drifts by up to 22.

Two sources, chosen by profile.ref_tex:
- "snapshot": the dataset's per-camera snapshot index (annotation_preview), used
  by the underwater line whose cameras appear in 50 synchronised snapshots;
- "video": frame 0 of each clip in --video-dir, used by lines that only have
  recordings.
"""
import argparse
from pathlib import Path

import cv2

from python.annotation_preview import common as C
from python.stitch import profiles as P
from python.stitch.profiles import StepError


def tex_names(profile):
    """The basenames `export` writes, in profile camera order.

    Shared with the renderer so the two never disagree about the naming rule."""
    return [f"{camera}.png" for camera in profile.camera_ids]


def first_frame(path):
    """Frame 0 of `path` as BGR uint8; a clip we cannot decode is fatal."""
    capture = cv2.VideoCapture(str(path))
    try:
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok or frame is None:
        raise StepError(f"cannot read the first frame of {path}")
    return frame


def _snapshot_frame(camera):
    """Frame 0 for `camera` from the dataset snapshot index, or None.

    C.frames_for_camera returns [(snapshot_id, path)] in snapshot time order, so
    element 0 is that camera's earliest frame."""
    frames = C.frames_for_camera(camera)
    if not frames:
        return None
    image = cv2.imread(str(frames[0][1]))
    if image is None:
        raise StepError(f"cannot read snapshot frame for {camera}: {frames[0][1]}")
    return image


def export(profile, out_dir=None, video_dir=None):
    """Write one `<camera_id>.png` per camera; return the paths in camera order."""
    out_dir = Path(out_dir) if out_dir is not None else profile.ref_tex_dir
    if profile.ref_tex == "video":
        if video_dir is None:
            raise StepError(
                f"profile {profile.name} takes reference textures from video; "
                "pass --video-dir")
    elif profile.ref_tex == "snapshot":
        if not Path(C.SNAP_DIR).is_dir():
            raise StepError(
                f"snapshot directory missing: {C.SNAP_DIR} "
                "(point ANNOTATION_PREVIEW_DATASET_ROOT at a valid dataset)")
    else:
        raise StepError(f"unknown ref_tex source: {profile.ref_tex!r}")

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for camera, name in zip(profile.camera_ids, tex_names(profile)):
        if profile.ref_tex == "video":
            source = profile.clip_for(video_dir, camera)
            image = first_frame(source)
        else:
            source = None
            image = _snapshot_frame(camera)
            if image is None:
                print(f"  {camera:9s} (no frames — skipped)")
                continue
        destination = out_dir / name
        if not cv2.imwrite(str(destination), image):
            raise StepError(f"cannot write {destination}")
        written.append(destination)
        origin = Path(source).name if source is not None else "snapshot"
        print(f"  {camera:9s} <- {origin}")
    if not written:
        raise StepError("no reference textures exported")
    print(f"wrote {len(written)} reference textures -> {out_dir}")
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export per-camera reference textures")
    ap.add_argument("--profile", default="underwater",
                    help="stitch line to export for (default: %(default)s)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="destination (default: <profile out_dir>/ref_tex)")
    ap.add_argument("--video-dir", type=Path, default=None,
                    help="clip directory, required when the profile's reference "
                         "textures come from video")
    args = ap.parse_args(argv)
    profile = P.get(args.profile)
    try:
        export(profile, out_dir=args.out_dir, video_dir=args.video_dir)
    except StepError as error:
        raise SystemExit(f"error: {error}")


if __name__ == "__main__":
    main()
