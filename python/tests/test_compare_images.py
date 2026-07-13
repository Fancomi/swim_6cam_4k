from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from python.validation.compare_images import (
    MAX_LOCAL_MAE,
    MAX_LOCAL_RMSE,
    MIN_PSNR,
    MIN_SSIM,
    compare,
    compute_global_ssim,
    passes_acceptance,
    run_cli,
)


LOGICAL_HEIGHT = 2101
LOGICAL_WIDTH = 5001
ENCODED_HEIGHT = 2102
ENCODED_WIDTH = 5002


class CompareImagesTest(unittest.TestCase):
    @staticmethod
    def _checker_reference():
        y = np.arange(LOGICAL_HEIGHT, dtype=np.uint16)[:, None]
        x = np.arange(LOGICAL_WIDTH, dtype=np.uint16)[None, :]
        checker = np.where((x + y) % 2 == 0, 64, 192).astype(np.uint8)
        return np.repeat(checker[..., None], 3, axis=2)

    @staticmethod
    def _candidate(reference, channels=3):
        candidate = np.zeros(
            (ENCODED_HEIGHT, ENCODED_WIDTH, channels), dtype=np.uint8
        )
        candidate[:LOGICAL_HEIGHT, :LOGICAL_WIDTH, :3] = reference
        if channels == 4:
            candidate[..., 3] = 255
        return candidate

    @staticmethod
    def _write_pair(directory, reference, candidate):
        reference_path = Path(directory) / "reference.png"
        candidate_path = Path(directory) / "candidate.png"
        if not cv2.imwrite(str(reference_path), reference):
            raise AssertionError("failed to write reference fixture")
        if not cv2.imwrite(str(candidate_path), candidate):
            raise AssertionError("failed to write candidate fixture")
        return reference_path, candidate_path

    def test_identical_image_has_perfect_metrics_and_valid_padding(self):
        reference = np.zeros(
            (LOGICAL_HEIGHT, LOGICAL_WIDTH, 3), dtype=np.uint8
        )
        candidate = self._candidate(reference, channels=4)
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_pair(directory, reference, candidate)
            metrics = compare(*paths)

        self.assertEqual(metrics["psnr"], float("inf"))
        self.assertAlmostEqual(metrics["ssim"], 1.0)
        self.assertTrue(passes_acceptance(metrics))
        self.assertAlmostEqual(compute_global_ssim(reference, reference), 1.0)

    def test_threshold_contract_is_stricter_than_original_global_gate(self):
        self.assertEqual(MIN_PSNR, 48.0)
        self.assertEqual(MIN_SSIM, 0.9995)
        self.assertEqual(MAX_LOCAL_MAE, 1.25)
        self.assertEqual(MAX_LOCAL_RMSE, 3.75)

    def test_cli_rejects_low_similarity_and_writes_amplified_difference(self):
        reference = np.zeros(
            (LOGICAL_HEIGHT, LOGICAL_WIDTH, 3), dtype=np.uint8
        )
        candidate = self._candidate(reference)
        candidate[:LOGICAL_HEIGHT, :LOGICAL_WIDTH] = 64
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_pair(directory, reference, candidate)
            exit_code = run_cli([str(path) for path in paths])

            self.assertEqual(exit_code, 1)
            difference = paths[1].with_name("candidate_diff.png")
            self.assertTrue(difference.is_file())
            diff_pixels = cv2.imread(str(difference), cv2.IMREAD_COLOR)
            self.assertTrue(np.all(diff_pixels == 255))

    def test_requires_exact_candidate_dimensions(self):
        reference = np.zeros(
            (LOGICAL_HEIGHT, LOGICAL_WIDTH, 3), dtype=np.uint8
        )
        for height, width in (
            (LOGICAL_HEIGHT, LOGICAL_WIDTH),
            (ENCODED_HEIGHT + 1, ENCODED_WIDTH + 1),
        ):
            with self.subTest(height=height, width=width):
                candidate = np.zeros((height, width, 3), dtype=np.uint8)
                with tempfile.TemporaryDirectory() as directory:
                    paths = self._write_pair(directory, reference, candidate)
                    with self.assertRaisesRegex(ValueError, "5002x2102"):
                        compare(*paths)

    def test_rejects_nonblack_or_nonopaque_padding(self):
        reference = np.zeros(
            (LOGICAL_HEIGHT, LOGICAL_WIDTH, 3), dtype=np.uint8
        )
        cases = (
            (3, (0, 5001, 0), 1),
            (3, (2101, 0, 1), 1),
            (4, (0, 5001, 3), 254),
            (4, (2101, 0, 3), 254),
        )
        for channels, index, value in cases:
            with self.subTest(channels=channels, index=index):
                candidate = self._candidate(reference, channels=channels)
                candidate[index] = value
                with tempfile.TemporaryDirectory() as directory:
                    paths = self._write_pair(directory, reference, candidate)
                    with self.assertRaisesRegex(ValueError, "padding"):
                        compare(*paths)

    def test_local_lines_fail_even_though_global_metrics_pass(self):
        reference = self._checker_reference()
        cases = (
            ("center", (1050, slice(0, LOGICAL_WIDTH))),
            ("last_column", (slice(0, LOGICAL_HEIGHT), 5000)),
        )
        for region_name, index in cases:
            with self.subTest(region=region_name):
                candidate = self._candidate(reference)
                corrupted = candidate[index].astype(np.int16) + 32
                candidate[index] = np.clip(corrupted, 0, 255)
                with tempfile.TemporaryDirectory() as directory:
                    paths = self._write_pair(directory, reference, candidate)
                    metrics = compare(*paths)

                    self.assertGreaterEqual(metrics["psnr"], MIN_PSNR)
                    self.assertGreaterEqual(metrics["ssim"], MIN_SSIM)
                    self.assertEqual(metrics["local"][region_name]["mae"], 32.0)
                    self.assertEqual(
                        metrics["local"][region_name]["rmse"], 32.0
                    )
                    self.assertFalse(passes_acceptance(metrics))
                    self.assertEqual(
                        run_cli([str(path) for path in paths]), 1
                    )
                    self.assertTrue(
                        paths[1].with_name("candidate_diff.png").is_file()
                    )

    def test_small_deterministic_error_passes_with_margin(self):
        reference = self._checker_reference()
        candidate = self._candidate(reference, channels=4)
        y = np.arange(LOGICAL_HEIGHT, dtype=np.int16)[:, None]
        x = np.arange(LOGICAL_WIDTH, dtype=np.int16)[None, :]
        noise = (x + 2 * y) % 3 - 1
        candidate[:LOGICAL_HEIGHT, :LOGICAL_WIDTH, :3] = np.clip(
            reference.astype(np.int16) + noise[..., None], 0, 255
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write_pair(directory, reference, candidate)
            metrics = compare(*paths)

        self.assertGreater(metrics["psnr"], 49.8)
        self.assertGreater(metrics["ssim"], 0.9999)
        for local in metrics["local"].values():
            self.assertLess(local["mae"], 0.67)
            self.assertLess(local["rmse"], 0.82)
        self.assertTrue(passes_acceptance(metrics))

    def test_rejects_neighbor_duplicated_outer_row_or_column(self):
        y = np.arange(LOGICAL_HEIGHT, dtype=np.uint16)[:, None]
        x = np.arange(LOGICAL_WIDTH, dtype=np.uint16)[None, :]
        pattern = ((3 * x + 5 * y) % 251).astype(np.uint8)
        reference = np.repeat(pattern[..., None], 3, axis=2)
        cases = (
            ("last_row", (2100, slice(0, 5000)), (2099, slice(0, 5000))),
            (
                "last_column",
                (slice(0, 2100), 5000),
                (slice(0, 2100), 4999),
            ),
        )
        for fingerprint, destination, source in cases:
            with self.subTest(fingerprint=fingerprint):
                candidate = self._candidate(reference)
                candidate[destination] = candidate[source]
                with tempfile.TemporaryDirectory() as directory:
                    paths = self._write_pair(directory, reference, candidate)
                    metrics = compare(*paths)

                self.assertTrue(metrics["duplicates"][fingerprint])
                self.assertFalse(passes_acceptance(metrics))


if __name__ == "__main__":
    unittest.main()
