"""Estimate one camera's drift between its calibration image and a new image.

The transform is returned in NORMALISED unit space (x/width, y/height), not
pixels. Two reasons: the calibration texture and the new frame need not share a
resolution — underwater's .fbm textures are 640x360 while the clips decode at
1280x720 — and UVs are themselves unit coordinates, so `mesh.warp_uv` can apply
the matrix without knowing either image's size.

Why not SIFT + RANSAC (the approach this was modelled on): the pool floor and
walls are periodic tiling. Feature matching finds hundreds of confident,
mutually-consistent matches one tile over. On underA1 and underA9 that produced
-116px and -106px of translation where the truth is ~0, and no amount of
displacement-consistency filtering catches it because the wrong answer IS
consistent. Direct intensity alignment cannot make that mistake as long as it
starts near the truth, which is what the phase-correlation seed provides.
"""
from dataclasses import dataclass

import cv2
import numpy as np

# Cheapest first. Translation is what a bumped camera mostly does; euclidean adds
# the small roll a knocked mount picks up; homography additionally absorbs the
# apparent shear a pan/tilt puts into a plane seen obliquely, which is why it
# wins on this data despite having the most freedom to go wrong.
MODELS = {
    "translation": cv2.MOTION_TRANSLATION,
    "euclidean": cv2.MOTION_EUCLIDEAN,
    "affine": cv2.MOTION_AFFINE,
    "homography": cv2.MOTION_HOMOGRAPHY,
}
DEFAULT_MODEL = "homography"

# Rejection thresholds. A calibration is worth more than a bad correction, so
# these are deliberately tighter than a general-purpose registration would use:
# what we are modelling is a camera that got knocked, not one that was re-aimed.
MAX_SCALE = 1.15            # and its reciprocal
MAX_ROTATION_DEG = 6.0
MAX_SHIFT = 0.15            # fraction of the frame, measured at the centre
MAX_PERSPECTIVE = 0.35      # |h20|, |h21| in unit space


class AlignError(ValueError):
    """The alignment inputs are unusable; the message is user-facing."""


@dataclass(frozen=True)
class Alignment:
    """One camera's drift, plus everything needed to judge it.

    `matrix` maps normalised calibration-image coordinates to normalised
    new-image coordinates, y DOWN from the top (image convention, not UV).

    `gain` is the honest verdict: NCC of the calibration image warped into the
    new one, minus the NCC of leaving it alone, measured over the region the warp
    covers. A transform that does not raise it is not a correction, whatever its
    parameters look like — that is the check `accepted` reports on.
    """
    matrix: tuple                    # 3x3, row-major, as nested tuples (JSON-safe)
    model: str
    ncc_before: float
    ncc_after: float
    shift_px: tuple                  # (dx, dy) at the frame centre, new-image pixels
    rotation_deg: float
    scale: tuple                     # (sx, sy)
    accepted: bool
    reason: str = ""                 # why not, when accepted is False

    @property
    def gain(self):
        return self.ncc_after - self.ncc_before

    @property
    def H(self):
        return np.array(self.matrix, dtype=np.float64)

    def as_dict(self):
        return {
            "matrix": [list(row) for row in self.matrix],
            "model": self.model,
            "ncc_before": self.ncc_before,
            "ncc_after": self.ncc_after,
            "gain": self.gain,
            "shift_px": list(self.shift_px),
            "rotation_deg": self.rotation_deg,
            "scale": list(self.scale),
            "accepted": self.accepted,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            matrix=tuple(tuple(float(v) for v in row) for row in data["matrix"]),
            model=data["model"],
            ncc_before=float(data["ncc_before"]),
            ncc_after=float(data["ncc_after"]),
            shift_px=tuple(float(v) for v in data["shift_px"]),
            rotation_deg=float(data["rotation_deg"]),
            scale=tuple(float(v) for v in data["scale"]),
            accepted=bool(data["accepted"]),
            reason=data.get("reason", ""),
        )


def _grey(image, size):
    """`image` as float32 grey in [0,1], resampled to (width, height)."""
    if image is None:
        raise AlignError("image is None")
    grey = (cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3
            else image)
    if (grey.shape[1], grey.shape[0]) != size:
        grey = cv2.resize(grey, size, interpolation=cv2.INTER_AREA)
    return grey.astype(np.float32) / 255.0


def ncc(first, second, mask=None):
    """Zero-mean normalised cross-correlation over `mask` (all pixels if None).

    `np.dot` rather than the `@` operator: on numpy 2.2 against macOS Accelerate,
    matmul on a large 1-D pair raises spurious "divide by zero encountered in
    matmul" RuntimeWarnings while returning the correct value. np.dot on the same
    inputs is silent, and a metric that prints a warning on every call trains
    people to ignore warnings."""
    a = first[mask] if mask is not None else first.ravel()
    b = second[mask] if mask is not None else second.ravel()
    if a.size < 100:
        return 0.0
    a = a.astype(np.float64) - a.mean()
    b = b.astype(np.float64) - b.mean()
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / norm) if norm > 0 else 0.0


def _pyramid(image, levels):
    stack = [image]
    for _ in range(levels - 1):
        stack.append(cv2.pyrDown(stack[-1]))
    return stack


def _seed_translation(reference, current):
    """A whole-image translation estimate, for ECC to start from.

    Phase correlation over the full frame rather than a local search: it answers
    "how far did everything move" in one FFT, and at the pyramid's coarsest level
    the periodic tiling has been blurred enough that the true peak wins."""
    window = cv2.createHanningWindow(
        (reference.shape[1], reference.shape[0]), cv2.CV_32F)
    (dx, dy), _response = cv2.phaseCorrelate(reference, current, window)
    return dx, dy


def _solve_pixels(reference, current, mode, levels, iterations, eps):
    """ECC coarse-to-fine, seeded by a phase correlation. Pixel-space 3x3."""
    reference_pyramid = _pyramid(reference, levels)
    current_pyramid = _pyramid(current, levels)
    homography = mode == cv2.MOTION_HOMOGRAPHY
    warp = (np.eye(3, dtype=np.float32) if homography
            else np.eye(2, 3, dtype=np.float32))
    top = len(reference_pyramid) - 1
    dx, dy = _seed_translation(reference_pyramid[top], current_pyramid[top])
    warp[0, 2], warp[1, 2] = dx, dy
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, iterations, eps)
    for level in range(top, -1, -1):
        try:
            _cc, warp = cv2.findTransformECC(
                reference_pyramid[level], current_pyramid[level], warp, mode,
                criteria, None, 5)
        except cv2.error:
            return None
        if level > 0:
            # Halving the pixel grid doubles a translation and halves the
            # perspective terms; the linear part is scale-free.
            warp = warp.copy()
            warp[0, 2] *= 2
            warp[1, 2] *= 2
            if homography:
                warp[2, 0] /= 2
                warp[2, 1] /= 2
    return (warp.astype(np.float64) if homography
            else np.vstack([warp, [0, 0, 1]]).astype(np.float64))


def describe(matrix, size):
    """(shift_px, rotation_deg, (sx, sy)) for a normalised matrix on `size`."""
    width, height = size
    linear = np.asarray(matrix, dtype=np.float64)[:2, :2]
    scale = (float(np.linalg.norm(linear[:, 0])),
             float(np.linalg.norm(linear[:, 1])))
    rotation = float(np.degrees(np.arctan2(linear[1, 0], linear[0, 0])))
    centre = np.asarray(matrix, dtype=np.float64) @ np.float64([0.5, 0.5, 1.0])
    centre = centre / centre[2]
    return ((float((centre[0] - 0.5) * width),
             float((centre[1] - 0.5) * height)),
            rotation, scale)


def sane(matrix):
    """(ok, reason) — is this plausibly a knocked camera rather than a mis-solve?

    Modelled on the reference implementation's homography validation, with the
    thresholds pulled in to what this rig can physically do. A wrong transform
    that passes these is still caught by the gain check in `solve`; the point of
    checking the parameters first is that the reason is legible in the report."""
    _shift, rotation, scale = describe(matrix, (1.0, 1.0))
    low, high = 1.0 / MAX_SCALE, MAX_SCALE
    if not (low < scale[0] < high and low < scale[1] < high):
        return False, f"scale {scale[0]:.3f},{scale[1]:.3f}"
    if abs(rotation) > MAX_ROTATION_DEG:
        return False, f"rotation {rotation:.2f}deg"
    matrix = np.asarray(matrix, dtype=np.float64)
    centre = matrix @ np.float64([0.5, 0.5, 1.0])
    centre = centre / centre[2]
    offset = max(abs(centre[0] - 0.5), abs(centre[1] - 0.5))
    if offset > MAX_SHIFT:
        return False, f"shift {offset:.3f} of frame"
    if (abs(matrix[2, 0]) > MAX_PERSPECTIVE
            or abs(matrix[2, 1]) > MAX_PERSPECTIVE):
        return False, (f"perspective {matrix[2, 0]:.3f},{matrix[2, 1]:.3f}")
    return True, ""


def solve(reference, current, model=DEFAULT_MODEL, levels=3, iterations=60,
          eps=1e-6):
    """One camera's drift from its calibration image to a new image.

    `reference` is the image the UVs were calibrated against, `current` the new
    one; both BGR uint8, sizes may differ. Returns an `Alignment` whose
    `accepted` says whether to use it — never None, because a refusal carries a
    reason worth reporting, and every caller has to handle both cases anyway.
    """
    if model not in MODELS:
        raise AlignError(f"unknown model {model!r}; "
                         f"valid: {', '.join(MODELS)}")
    height, width = current.shape[:2]
    size = (width, height)
    reference_grey = _grey(reference, size)
    current_grey = _grey(current, size)

    pixels = _solve_pixels(reference_grey, current_grey, MODELS[model], levels,
                           iterations, eps)
    if pixels is None:
        return Alignment(matrix=((1, 0, 0), (0, 1, 0), (0, 0, 1)), model=model,
                         ncc_before=ncc(reference_grey, current_grey),
                         ncc_after=0.0, shift_px=(0.0, 0.0), rotation_deg=0.0,
                         scale=(1.0, 1.0), accepted=False,
                         reason="ECC did not converge")

    # Score in pixel space, where the warp actually happened, and only over the
    # pixels the warp covers: a translation slides part of the frame out, and
    # scoring the vacated band against black would penalise the very shift that
    # is correct.
    warped = cv2.warpPerspective(reference_grey, pixels, size)
    covered = cv2.warpPerspective(np.ones_like(reference_grey), pixels, size,
                                  flags=cv2.INTER_NEAREST) > 0
    before = ncc(reference_grey, current_grey, covered)
    after = ncc(warped, current_grey, covered)

    scaling = np.diag([width, height, 1.0])
    matrix = np.linalg.inv(scaling) @ pixels @ scaling
    shift, rotation, scale = describe(matrix, size)
    ok, reason = sane(matrix)
    if ok and after <= before:
        ok, reason = False, f"no gain ({after - before:+.4f})"
    return Alignment(
        matrix=tuple(tuple(float(v) for v in row) for row in matrix),
        model=model, ncc_before=before, ncc_after=after, shift_px=shift,
        rotation_deg=rotation, scale=scale, accepted=ok, reason=reason)
