"""Repository layout: the invariants a new agent should be able to rely on."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# One package per task, plus python/common for what more than one of them needs.
PACKAGES = {
    "common": "paths, media I/O, CSV tables, HTML pages",
    "fbx_tools": "the only place that imports the FBX SDK",
    "stitch": "the three camera lines: pool, underwater, overhead",
    "water_entry": "the single water-entry camera",
    "labeling": "browser labelers over the dataset snapshots",
    "keypoints": "COCO-17 annotation review pages",
    "benchmarks": "runtime metrics validation and summaries",
}


class LayoutTest(unittest.TestCase):
    def test_languages_stay_separated(self):
        self.assertFalse((ROOT / "src").exists())
        self.assertTrue((ROOT / "cpp" / "core").is_dir())
        self.assertTrue((ROOT / "python").is_dir())

    def test_every_package_exists_and_documents_itself(self):
        for name in PACKAGES:
            init = ROOT / "python" / name / "__init__.py"
            self.assertTrue(init.is_file(), name)
            self.assertTrue(init.read_text(encoding="utf-8").startswith('"""'),
                            f"python/{name} has no module docstring")

    def test_no_package_beyond_the_declared_set(self):
        found = {path.name for path in (ROOT / "python").iterdir()
                 if path.is_dir() and not path.name.startswith("__")}
        self.assertEqual(found, set(PACKAGES))

    def test_the_fbx_sdk_is_imported_in_exactly_one_package(self):
        # Autodesk only ships the wheel for cp310, so anything that reads an
        # already-extracted mesh JSON must keep working without it.
        importers = sorted(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "python").rglob("*.py")
            if "import fbx" in path.read_text(encoding="utf-8"))
        self.assertEqual(importers, ["python/fbx_tools/bake_uv.py",
                                     "python/fbx_tools/scene.py"])

    def test_paths_are_derived_from_one_root(self):
        from python.common import paths
        self.assertEqual(paths.PROJECT_ROOT, ROOT)
        for value in (paths.INPUTS, paths.OUTPUTS, paths.CONFIGS,
                      paths.GENERATED):
            self.assertTrue(value.is_relative_to(ROOT), value)

    def test_no_module_recomputes_the_repository_root(self):
        # Ten modules used to carry their own parents[2] chain; moving one a level
        # deeper then pointed it at the wrong tree.
        offenders = [path.relative_to(ROOT).as_posix()
                     for path in (ROOT / "python").rglob("*.py")
                     if "parents[2]" in path.read_text(encoding="utf-8")
                     and path.name != "paths.py"]
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
