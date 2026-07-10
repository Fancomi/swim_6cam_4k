from pathlib import Path
import tempfile
import unittest

import cv2
import numpy as np

from python.validation.compare_images import (
    compare,
    compute_global_ssim,
    run_cli,
)


class CompareImagesTest(unittest.TestCase):
    def test_identical_images_have_perfect_metrics_and_ignore_padding(self):
        reference = np.arange(6 * 7 * 3, dtype=np.uint8).reshape(6, 7, 3)
        candidate = np.zeros((8, 9, 3), dtype=np.uint8)
        candidate[:6, :7] = reference
        with tempfile.TemporaryDirectory() as directory:
            reference_path = Path(directory) / "reference.png"
            candidate_path = Path(directory) / "candidate.png"
            self.assertTrue(cv2.imwrite(str(reference_path), reference))
            self.assertTrue(cv2.imwrite(str(candidate_path), candidate))

            metrics = compare(reference_path, candidate_path)

        self.assertEqual(metrics["psnr"], float("inf"))
        self.assertAlmostEqual(metrics["ssim"], 1.0)
        self.assertAlmostEqual(compute_global_ssim(reference, reference), 1.0)

    def test_cli_rejects_low_similarity_and_writes_amplified_difference(self):
        reference = np.zeros((4, 4, 3), dtype=np.uint8)
        candidate = np.full((4, 4, 3), 64, dtype=np.uint8)
        with tempfile.TemporaryDirectory() as directory:
            reference_path = Path(directory) / "reference.png"
            candidate_path = Path(directory) / "candidate.png"
            self.assertTrue(cv2.imwrite(str(reference_path), reference))
            self.assertTrue(cv2.imwrite(str(candidate_path), candidate))

            exit_code = run_cli([str(reference_path), str(candidate_path)])

            self.assertEqual(exit_code, 1)
            difference = candidate_path.with_name("candidate_diff.png")
            self.assertTrue(difference.is_file())
            diff_pixels = cv2.imread(str(difference), cv2.IMREAD_COLOR)
            self.assertTrue(np.all(diff_pixels == 255))


if __name__ == "__main__":
    unittest.main()
