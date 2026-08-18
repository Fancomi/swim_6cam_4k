"""python/align: the drift correction, on synthetic images.

Synthetic rather than sampled: the dataset is not in git, and the properties that
matter — recover a known shift, refuse an implausible one, move UVs by exactly
the right number of pixels — are all statable without it. The numbers measured on
the real data live in the docstrings of the modules that produced them.
"""
import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from python.align import cache as C
from python.align import mesh as M
from python.align.aligner import (Alignment, DEFAULT_MODEL, MODELS, AlignError,
                                  ncc, sane, solve)


def texture(width=320, height=200, seed=7):
    """A synthetic scene with real structure and no repeats.

    Two properties matter, and both were learned by getting them wrong:

    Contrast. Blurred noise looks like a fine test image and is not — at sigma 3
    its grey standard deviation is 0.018, and ECC's gradient descent stalls on it
    ("did not converge") for half the shifts tried. Filled rectangles and lines
    give 0.21, which is the order the real pool frames carry.

    No periodicity. Repeating texture is precisely what defeats feature matching
    on the real data, so a tiled fixture would let a false-locking estimator pass
    the suite.
    """
    rng = np.random.default_rng(seed)
    image = np.full((height, width, 3), 40, np.uint8)
    for _ in range(70):
        x, y = int(rng.integers(0, width)), int(rng.integers(0, height))
        w, h = int(rng.integers(6, 50)), int(rng.integers(6, 50))
        cv2.rectangle(image, (x, y), (x + w, y + h),
                      tuple(int(v) for v in rng.integers(60, 255, 3)), -1)
    for _ in range(30):
        start = (int(rng.integers(0, width)), int(rng.integers(0, height)))
        end = (int(rng.integers(0, width)), int(rng.integers(0, height)))
        cv2.line(image, start, end,
                 tuple(int(v) for v in rng.integers(0, 255, 3)), 2)
    return cv2.GaussianBlur(image, (0, 0), 1.0)


# How much bigger the source is than the crops taken from it, so a shifted crop
# is filled with real content rather than a synthesised border.
MARGIN = 60


def shifted_pair(dx, dy, width=320, height=200, seed=7):
    """Two crops of one scene, the second taken (dx, dy) further along.

    Cropping rather than warping with a border mode. A reflected or replicated
    border is mirror-symmetric content that exists in neither real image, and a
    homography will happily fit it — measured here as a spurious 1.43x scale for
    a pure translation. Two crops are a true translation with genuine content
    across the whole frame.

    Note the sign: taking the second crop (dx, dy) further along moves the
    CONTENT by (-dx, -dy), which is what an aligner reports.
    """
    source = texture(width + 2 * MARGIN, height + 2 * MARGIN, seed)

    def crop(offset_x, offset_y):
        x, y = MARGIN + offset_x, MARGIN + offset_y
        return source[y:y + height, x:x + width].copy()

    return crop(0, 0), crop(dx, dy)


def flat_mesh(u0=0.2, u1=0.8, v0=0.3, v1=0.7):
    """A two-triangle mesh with UVs spanning a rectangle."""
    def vertex(u, v):
        return {"pos": [u * 10.0, v * 10.0], "uv": [u, v]}
    return {"node": "T", "texture_basename": "t.png",
            "triangles": [[vertex(u0, v0), vertex(u1, v0), vertex(u1, v1)],
                          [vertex(u0, v0), vertex(u1, v1), vertex(u0, v1)]]}


def translation(dx, dy, size):
    """A normalised 3x3 translating by (dx, dy) pixels on `size`."""
    width, height = size
    return np.array([[1, 0, dx / width], [0, 1, dy / height], [0, 0, 1]],
                    dtype=np.float64)


class SolveTest(unittest.TestCase):
    def test_recovers_a_known_translation(self):
        for dx, dy in ((7, 0), (0, -5), (-11, 4)):
            reference, current = shifted_pair(dx, dy)
            alignment = solve(reference, current, "translation")
            self.assertTrue(alignment.accepted, alignment.reason)
            # The crop moves by (dx, dy), so the CONTENT moves the other way.
            self.assertAlmostEqual(alignment.shift_px[0], -dx, delta=0.5)
            self.assertAlmostEqual(alignment.shift_px[1], -dy, delta=0.5)

    def test_every_model_recovers_the_same_translation(self):
        """The freer models must not invent rotation or scale to explain a shift.

        This is the check that caught a synthetic-data mistake worth keeping in
        mind: warping with a reflected border gave homography a mirror-symmetric
        edge to fit, and it reported a 1.43x scale for a pure translation."""
        reference, current = shifted_pair(9, -3)
        for model in MODELS:
            alignment = solve(reference, current, model)
            self.assertTrue(alignment.accepted, f"{model}: {alignment.reason}")
            self.assertAlmostEqual(alignment.shift_px[0], -9, delta=1.0, msg=model)
            self.assertAlmostEqual(alignment.shift_px[1], 3, delta=1.0, msg=model)
            self.assertAlmostEqual(alignment.rotation_deg, 0.0, delta=1.0,
                                   msg=model)
            self.assertAlmostEqual(alignment.scale[0], 1.0, delta=0.05, msg=model)

    def test_identical_images_need_no_correction(self):
        """A camera that did not move must not be "corrected" anyway.

        The gate is the gain check, not the parameters: a near-identity transform
        would pass sane() easily, and applying it would add sub-pixel resampling
        blur to every frame for nothing."""
        reference = texture()
        alignment = solve(reference, reference.copy(), DEFAULT_MODEL)
        self.assertFalse(alignment.accepted)
        self.assertIn("no gain", alignment.reason)
        self.assertGreater(alignment.ncc_before, 0.99)

    def test_reference_and_current_may_differ_in_size(self):
        """The .fbm textures are 640x360 while the clips decode at 1280x720."""
        reference, current = shifted_pair(-12, 0)
        current = cv2.resize(current, (640, 400))
        alignment = solve(reference, current, "translation")
        self.assertTrue(alignment.accepted, alignment.reason)
        # The shift is reported in the CURRENT image's pixels, so a reference
        # scaled up by two reports twice the shift it was built with.
        self.assertAlmostEqual(alignment.shift_px[0], 24, delta=2.0)

    def test_unrelated_images_are_refused_or_gain_nothing(self):
        alignment = solve(texture(seed=1), texture(seed=2), DEFAULT_MODEL)
        self.assertFalse(alignment.accepted)

    def test_an_unknown_model_is_an_error(self):
        with self.assertRaises(AlignError):
            solve(texture(), texture(), "projective")


class SaneTest(unittest.TestCase):
    def test_the_identity_is_sane(self):
        ok, reason = sane(np.eye(3))
        self.assertTrue(ok, reason)

    def test_a_large_rotation_is_refused(self):
        angle = np.radians(20.0)
        matrix = np.array([[np.cos(angle), -np.sin(angle), 0],
                           [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
        ok, reason = sane(matrix)
        self.assertFalse(ok)
        self.assertIn("rotation", reason)

    def test_a_large_scale_is_refused(self):
        ok, reason = sane(np.diag([1.5, 1.0, 1.0]))
        self.assertFalse(ok)
        self.assertIn("scale", reason)

    def test_a_large_shift_is_refused(self):
        """A quarter-frame jump is a re-aimed camera, not a knocked one."""
        matrix = np.array([[1, 0, 0.25], [0, 1, 0], [0, 0, 1]], dtype=float)
        ok, reason = sane(matrix)
        self.assertFalse(ok)
        self.assertIn("shift", reason)

    def test_a_strong_perspective_term_is_refused(self):
        matrix = np.array([[1, 0, 0], [0, 1, 0], [0.5, 0, 1]], dtype=float)
        ok, reason = sane(matrix)
        self.assertFalse(ok)
        self.assertIn("perspective", reason)


class NccTest(unittest.TestCase):
    def test_an_image_correlates_perfectly_with_itself(self):
        grey = cv2.cvtColor(texture(), cv2.COLOR_BGR2GRAY).astype(np.float32)
        self.assertAlmostEqual(ncc(grey, grey), 1.0, places=6)

    def test_brightness_and_contrast_do_not_change_it(self):
        """Zero-mean and normalised, so a session's exposure change is invisible.

        This matters: the calibration texture and a new frame are months apart
        under different pool lighting."""
        grey = cv2.cvtColor(texture(), cv2.COLOR_BGR2GRAY).astype(np.float32)
        self.assertAlmostEqual(ncc(grey, grey * 0.6 + 40.0), 1.0, places=5)

    def test_a_mask_restricts_the_comparison(self):
        grey = cv2.cvtColor(texture(), cv2.COLOR_BGR2GRAY).astype(np.float32)
        other = grey.copy()
        other[:, 160:] = 0
        mask = np.zeros(grey.shape, bool)
        mask[:, :160] = True
        self.assertAlmostEqual(ncc(grey, other, mask), 1.0, places=6)
        self.assertLess(ncc(grey, other), 1.0)


class WarpUvTest(unittest.TestCase):
    def test_the_identity_leaves_uvs_untouched(self):
        original = flat_mesh()
        warped = M.warp_uv(original, np.eye(3))
        for before, after in zip(original["triangles"], warped["triangles"]):
            for first, second in zip(before, after):
                self.assertAlmostEqual(first["uv"][0], second["uv"][0], places=9)
                self.assertAlmostEqual(first["uv"][1], second["uv"][1], places=9)

    def test_it_does_not_mutate_its_input(self):
        original = flat_mesh()
        snapshot = json.dumps(original)
        M.warp_uv(original, translation(10, 10, (100, 100)))
        self.assertEqual(json.dumps(original), snapshot)

    def test_positions_are_never_touched(self):
        """The pool did not move — only the camera did."""
        original = flat_mesh()
        warped = M.warp_uv(original, translation(25, -13, (640, 360)))
        for before, after in zip(original["triangles"], warped["triangles"]):
            for first, second in zip(before, after):
                self.assertEqual(first["pos"], second["pos"])

    def test_a_pixel_translation_moves_uvs_by_that_many_pixels(self):
        size = (640, 360)
        original = flat_mesh()
        warped = M.warp_uv(original, translation(8, 5, size))
        mean, median, largest = M.uv_shift_px(original, warped, size)
        expected = float(np.hypot(8, 5))
        self.assertAlmostEqual(mean, expected, places=4)
        self.assertAlmostEqual(median, expected, places=4)
        self.assertAlmostEqual(largest, expected, places=4)

    def test_v_flips_so_a_downward_shift_lowers_the_uv(self):
        """The one place image coordinates and UV coordinates disagree.

        A matrix that moves things DOWN the image (+dy) must DECREASE v, because
        v=0 is the image bottom. Getting this backwards yields a correction of the
        right size in the wrong direction, which reads as the alignment having
        made things worse."""
        original = flat_mesh()
        warped = M.warp_uv(original, translation(0, 18, (100, 100)))
        first_before = original["triangles"][0][0]["uv"][1]
        first_after = warped["triangles"][0][0]["uv"][1]
        self.assertLess(first_after, first_before)
        self.assertAlmostEqual(first_before - first_after, 0.18, places=6)

    def test_uvs_are_not_clamped(self):
        """The underwater planes overhang their images by design."""
        original = flat_mesh(u0=0.0, u1=1.0)
        warped = M.warp_uv(original, translation(50, 0, (100, 100)))
        us = [vertex["uv"][0] for triangle in warped["triangles"]
              for vertex in triangle]
        self.assertGreater(max(us), 1.0)


class WarpMeshesTest(unittest.TestCase):
    def accepted(self, dx, dy, size=(100, 100)):
        matrix = translation(dx, dy, size)
        return Alignment(matrix=tuple(tuple(row) for row in matrix),
                         model="translation", ncc_before=0.5, ncc_after=0.7,
                         shift_px=(dx, dy), rotation_deg=0.0, scale=(1.0, 1.0),
                         accepted=True)

    def rejected(self):
        alignment = self.accepted(10, 10)
        return Alignment(**{**alignment.__dict__, "accepted": False,
                            "reason": "no gain"})

    def test_no_alignments_returns_the_meshes_unchanged(self):
        meshes = [flat_mesh(), flat_mesh()]
        self.assertEqual(M.warp_meshes(meshes, None), meshes)

    def test_a_rejected_alignment_leaves_that_mesh_as_calibrated(self):
        """Per-camera fallback: fifteen good corrections are not thrown away
        because the sixteenth camera's view was too disturbed to register."""
        meshes = [flat_mesh(), flat_mesh()]
        result = M.warp_meshes(meshes, [self.accepted(10, 0), self.rejected()])
        self.assertNotEqual(result[0]["triangles"], meshes[0]["triangles"])
        self.assertEqual(result[1], meshes[1])

    def test_a_missing_alignment_leaves_that_mesh_alone(self):
        meshes = [flat_mesh(), flat_mesh()]
        result = M.warp_meshes(meshes, [None, self.accepted(10, 0)])
        self.assertEqual(result[0], meshes[0])
        self.assertNotEqual(result[1]["triangles"], meshes[1]["triangles"])

    def test_a_length_mismatch_is_an_error(self):
        with self.assertRaises(ValueError):
            M.warp_meshes([flat_mesh()], [self.accepted(1, 1), None])

    def test_uv_shift_px_rejects_meshes_of_different_size(self):
        with self.assertRaises(ValueError):
            M.uv_shift_px(flat_mesh(),
                          {"triangles": flat_mesh()["triangles"][:1]},
                          (100, 100))


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "align.json"
        self.reference, self.current = shifted_pair(6, 0)

    def tearDown(self):
        self.directory.cleanup()

    def resolve(self, force=False, current=None):
        return C.resolve("line", ["cam"], {"cam": self.reference},
                         {"cam": current if current is not None else self.current},
                         model="translation", cache_path=self.path, force=force)

    def test_a_solved_alignment_round_trips(self):
        first = self.resolve()["cam"]
        second = self.resolve()["cam"]
        self.assertEqual(first.as_dict(), second.as_dict())
        self.assertTrue(self.path.is_file())

    def test_changed_pixels_invalidate_the_entry(self):
        """Fingerprints are over the pixels, because these textures get re-baked
        in place without ever changing name."""
        self.resolve()
        stored = json.loads(self.path.read_text(encoding="utf-8"))
        key = stored["cameras"]["cam"]["key"]
        self.resolve(current=shifted_pair(-9, 2)[1])
        reloaded = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertNotEqual(reloaded["cameras"]["cam"]["key"], key)

    def test_identical_pixels_in_a_different_encoding_reuse_the_entry(self):
        self.resolve()
        key = json.loads(self.path.read_text())["cameras"]["cam"]["key"]
        self.assertEqual(C.fingerprint(self.current.copy()),
                         C.fingerprint(self.current))
        self.resolve()
        self.assertEqual(json.loads(self.path.read_text())["cameras"]["cam"]["key"],
                         key)

    def test_a_foreign_format_is_discarded_rather_than_misread(self):
        self.path.write_text(json.dumps({"format": "align/v0", "cameras": {}}),
                             encoding="utf-8")
        self.assertIsNone(C.load(self.path))
        self.assertIsNotNone(self.resolve()["cam"])

    def test_unreadable_json_is_discarded(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(C.load(self.path))

    def test_a_camera_without_a_probe_gets_no_alignment(self):
        result = C.resolve("line", ["cam", "other"], {"cam": self.reference},
                           {"cam": self.current}, model="translation")
        self.assertIsNotNone(result["cam"])
        self.assertIsNone(result["other"])

    def test_report_rows_cover_every_camera(self):
        result = C.resolve("line", ["cam", "other"], {"cam": self.reference},
                           {"cam": self.current}, model="translation")
        rows = C.report_rows(["cam", "other"], result)
        self.assertEqual([row["camera"] for row in rows], ["cam", "other"])
        self.assertEqual(rows[1]["reason"], "no probe")
        self.assertEqual(set(rows[0]), set(C.REPORT_COLUMNS))


if __name__ == "__main__":
    unittest.main()
