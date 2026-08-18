"""Turn "the new data" into the one image an alignment is measured against.

A clip is not an image, and its first frame is the wrong one to use: the
underwater samples start with a swimmer already in shot, and registering against
splash pulls the transform towards the swimmer instead of the pool. A median over
frames spread across the clip removes anything that moves and leaves the fixed
scene — the same reasoning the labeling chain's median backgrounds use, at a much
smaller sample count because nine frames is enough to out-vote a swimmer at any
one pixel and the whole point is that this runs in under a second per camera.

Nine samples costs ~0.35s per 1280x720 clip here, and the transform it yields is
stable: re-probing with different sample counts moves the recovered shift by well
under a pixel.
"""
from pathlib import Path

import numpy as np

from python.common.media import MediaError, read_frames, read_image

DEFAULT_SAMPLES = 9
# Any file the recorder or the dataset might hand us. Checked in this order.
CLIP_SUFFIXES = (".ts", ".mp4", ".mkv", ".mov")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp")


class ProbeError(MediaError):
    """No usable probe image for a camera; the message is user-facing."""


def median_probe(clip, samples=DEFAULT_SAMPLES):
    """The fixed scene of `clip`: a per-pixel median over `samples` frames.

    Frames are taken across the whole clip rather than from the start, so a
    swimmer who is in shot for half of it still loses the vote."""
    import cv2

    capture = cv2.VideoCapture(str(clip))
    try:
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        capture.release()
    if total <= 0:
        # A .ts written by the packet-copy recorder can report no frame count;
        # sampling the first `samples` frames is still better than one frame.
        indices = list(range(samples))
    else:
        indices = np.linspace(0, total - 1, samples).astype(int).tolist()
    frames = read_frames(clip, indices)
    if not frames:
        raise ProbeError(f"no decodable frames in {clip}")
    stack = np.stack([frames[key] for key in sorted(frames)])
    return np.median(stack, axis=0).astype(np.uint8)


def image_probe(path):
    """A per-camera image used directly (a dataset's median background)."""
    return read_image(path, "probe image")


def find_source(directory, camera, pattern=None):
    """The one file in `directory` belonging to `camera`, clip or image.

    A directory of clips names them `<sample>_<camera>.ts`; a directory of
    per-camera images names them `<camera>_background.png` or similar. Both are
    legitimate "here is what the camera sees now", so one lookup serves both and
    the caller does not have to know which kind of directory it was handed.

    Ambiguity is an error, not a coin toss: two candidates for one camera means
    aligning against the wrong session, which shows up as a plausible-looking
    transform that makes everything worse."""
    directory = Path(directory)
    if not directory.is_dir():
        raise ProbeError(f"probe directory does not exist: {directory}")
    if pattern:
        candidates = sorted(directory.glob(pattern.format(camera=camera)))
    else:
        # `*_<camera>.<ext>` for clips, `<camera>*.<ext>` for per-camera images.
        # The camera id is anchored either way, so underA1 cannot match underA16.
        candidates = []
        for suffix in CLIP_SUFFIXES:
            candidates += sorted(directory.glob(f"*_{camera}{suffix}"))
        for suffix in IMAGE_SUFFIXES:
            candidates += sorted(directory.glob(f"{camera}{suffix}"))
            candidates += sorted(directory.glob(f"{camera}_*{suffix}"))
    if not candidates:
        raise ProbeError(f"no clip or image for {camera} in {directory}")
    if len(candidates) > 1:
        raise ProbeError(f"ambiguous probe sources for {camera}: "
                         f"{[c.name for c in candidates]}")
    return candidates[0]


def probe(path, samples=DEFAULT_SAMPLES):
    """One probe image from a clip or an image file, by suffix."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in CLIP_SUFFIXES:
        return median_probe(path, samples)
    if suffix in IMAGE_SUFFIXES:
        return image_probe(path)
    raise ProbeError(f"cannot probe {path.name}: unknown kind")


def probe_cameras(directory, cameras, pattern=None, samples=DEFAULT_SAMPLES):
    """{camera: probe image} for every camera that has a source in `directory`.

    A camera with no source is omitted rather than fatal: the caller falls back
    to that camera's original calibration, which is exactly the behaviour a
    missing clip should produce."""
    found = {}
    for camera in cameras:
        try:
            source = find_source(directory, camera, pattern)
        except ProbeError as error:
            print(f"  {camera:12s} (skipped: {error})")
            continue
        found[camera] = probe(source, samples)
        print(f"  {camera:12s} <- {source.name}")
    return found
