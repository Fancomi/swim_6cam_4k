import os
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class BenchmarkScriptsTest(unittest.TestCase):
    def test_matrix_runner_lists_each_required_cell_once(self):
        script = ROOT / "scripts" / "run_metal_benchmarks.sh"
        result = subprocess.run(
            [str(script), "--list-cells"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        cells = result.stdout.splitlines()
        self.assertEqual(len(cells), 48)
        self.assertEqual(len(set(cells)), 48)
        self.assertIn("decode-only,1,paced", cells)
        self.assertIn("full,6,unpaced", cells)
        self.assertTrue(os.access(script, os.X_OK))

    def test_soak_help_exposes_safety_defaults(self):
        script = ROOT / "scripts" / "run_metal_soak.sh"
        result = subprocess.run(
            [str(script), "--help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        self.assertIn("600", result.stdout)
        self.assertIn("29.0", result.stdout)
        self.assertTrue(os.access(script, os.X_OK))
