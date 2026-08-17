"""Image and video I/O that fails loudly.

`cv2.imwrite` returns False instead of raising, and seven call sites used to
each decide what to do about that — three of them ignored it entirely, so a
full disk or a missing directory produced a silent no-op. One helper that
raises, plus one reader that refuses to return None, removes that class of bug.

Both helpers go through the codec on a byte buffer and let Python open the file,
rather than handing the path to OpenCV. `cv2.imread`/`imwrite` open through the
C runtime's narrow API, which on Windows resolves the name in the system ANSI
code page — so a perfectly ordinary path like `inputs/overhead/models/25 水面.fbm`
fails with "can't open/read file: check file path/integrity" on a machine whose
ACP is not UTF-8, while a copy of the identical bytes at an ASCII path reads
fine. The failure names the path, so it reads like a missing file rather than an
encoding problem.
"""
import shutil
import subprocess
from pathlib import Path

import cv2
import numpy as np


class MediaError(RuntimeError):
    """An image or clip could not be read or written; message is user-facing."""


def write_image(path, image, kind="image"):
    """Write `image` to `path`, creating parents. Raises on any failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ok, buffer = cv2.imencode(path.suffix, image)
    except cv2.error as error:
        raise MediaError(f"cannot write {kind}: {path}: {error}") from None
    if not ok:
        raise MediaError(f"cannot write {kind}: {path}")
    try:
        path.write_bytes(buffer.tobytes())
    except OSError as error:
        raise MediaError(f"cannot write {kind}: {path}: {error}") from None
    return path


def read_image(path, kind="image"):
    """Decode `path` as BGR uint8. Raises rather than returning None."""
    path = Path(path)
    try:
        data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    except OSError as error:
        raise MediaError(f"cannot read {kind}: {path}: {error}") from None
    image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if image is None:
        raise MediaError(f"cannot read {kind}: {path}")
    return image


def first_frame(path):
    """Frame 0 of a clip as BGR uint8."""
    capture = cv2.VideoCapture(str(path))
    try:
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok or frame is None:
        raise MediaError(f"cannot read the first frame of {path}")
    return frame


def read_frames(path, indices):
    """Decode the given frame numbers, returning {index: BGR ndarray}.

    Sequential `grab()` to each target rather than seeking by
    CAP_PROP_POS_FRAMES: on the recorded clips here the keyframe index is
    unreliable, and a wrong seek returns a neighbouring frame silently. Stops
    early — and returns what it has — when the clip ends before the last
    requested index."""
    want = sorted({int(index) for index in indices})
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise MediaError(f"cannot open clip: {path}")
    frames, cursor, position = {}, 0, 0
    try:
        while cursor < len(want):
            target = want[cursor]
            while position < target:
                if not capture.grab():
                    return frames
                position += 1
            ok, frame = capture.read()
            if not ok:
                return frames
            frames[target] = frame
            position += 1
            cursor += 1
    finally:
        capture.release()
    return frames


def open_encoder(out_path, width, height, fps):
    """ffmpeg reading raw bgr24 from stdin, writing h264 yuv420p."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaError("ffmpeg is required for video rendering")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{width}x{height}", "-r", f"{fps:.4f}", "-i", "-",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        # yuv420p needs even dimensions; pad rather than rescale so no pixel moves.
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-pix_fmt", "yuv420p", str(out_path),
    ]
    return subprocess.Popen(command, stdin=subprocess.PIPE)


def close_encoder(encoder, out_path):
    """Close stdin, wait, and raise if ffmpeg or the pipe failed.

    Both halves matter: a non-zero exit means ffmpeg rejected something, while a
    broken pipe means it died mid-stream and the file is truncated. Reporting
    only the exit code would let a partial mp4 pass as complete."""
    pipe_error = None
    if encoder.stdin is not None and not encoder.stdin.closed:
        try:
            encoder.stdin.close()
        except OSError as error:
            pipe_error = error
    code = encoder.wait()
    if code != 0:
        raise MediaError(f"ffmpeg failed for {out_path}: exit code {code}")
    if pipe_error is not None:
        raise MediaError(f"ffmpeg pipe failed for {out_path}: {pipe_error}")
