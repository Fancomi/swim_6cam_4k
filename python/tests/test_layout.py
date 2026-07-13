from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class LayoutTest(unittest.TestCase):
    def test_languages_are_isolated(self):
        self.assertFalse((ROOT / "src").exists())
        self.assertTrue((ROOT / "python/assets/extract_fbx.py").is_file())
        self.assertTrue((ROOT / "python/validation/reference_renderer.py").is_file())

    def test_reference_renderer_uses_repository_root(self):
        from python.validation import reference_renderer
        self.assertEqual(reference_renderer.PROJECT_ROOT, ROOT)


if __name__ == "__main__":
    unittest.main()
