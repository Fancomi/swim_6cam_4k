"""Export one reference texture per camera: the frame the stitch really sees.

The textures a designer bakes into the .fbm carry calibration overlays (yellow
lane lines, distance labels) — what you want when checking geometry, not when
judging image quality. This writes each camera's first captured frame instead, so
`still --real` stitches photographic imagery by swapping one directory.

Files are named `<camera_id>.png`, not after the mesh's texture_basename:
- the overhead basenames (05-02.jpg, C06.jpg) name a designer's working file, so
  a directory full of them says nothing about which camera is which;
- reusing a `.jpg` basename would re-encode a lossless decode as JPEG. Measured
  on cam5: max channel error 35, and the stitch drifts by up to 22.

Two sources, chosen by profile.ref_tex — "video" reads frame 0 of each clip,
"snapshot" reads the dataset's per-camera snapshot index (the underwater cameras
appear in 50 synchronised snapshots and have no per-run clip to sample).
"""
from python.common.media import first_frame, read_image, write_image
from python.stitch.profiles import StepError


def tex_names(profile):
    """The basenames `export` writes, in profile camera order.

    Shared with the renderer so the two cannot disagree about the naming rule."""
    return [f"{camera}.png" for camera in profile.camera_ids]


def _snapshot(camera):
    """This camera's earliest dataset snapshot frame, or None if it has none."""
    from python.labeling.snapshots import frames_for_camera
    frames = frames_for_camera(camera)
    if not frames:
        return None
    return read_image(frames[0][1], f"snapshot frame for {camera}")


def export(profile, out_dir=None, video_dir=None):
    """Write one `<camera_id>.png` per camera; return the paths in camera order."""
    out_dir = out_dir or profile.ref_tex_dir
    if profile.ref_tex == "video" and video_dir is None:
        raise StepError(f"{profile.name} takes reference textures from video; "
                        "pass --video-dir")
    if profile.ref_tex not in ("video", "snapshot"):
        raise StepError(f"unknown ref_tex source: {profile.ref_tex!r}")

    written = []
    for camera, name in zip(profile.camera_ids, tex_names(profile)):
        if profile.ref_tex == "video":
            source = profile.clip_for(video_dir, camera)
            image, origin = first_frame(source), source.name
        else:
            image, origin = _snapshot(camera), "snapshot"
            if image is None:
                print(f"  {camera:10s} (no frames — skipped)")
                continue
        written.append(write_image(out_dir / name, image, "reference texture"))
        print(f"  {camera:10s} <- {origin}")
    if not written:
        raise StepError("no reference textures exported")
    print(f"wrote {len(written)} reference textures -> {out_dir}")
    return written
