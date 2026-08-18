"""Repository layout: the invariants a new agent should be able to rely on."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# One package per task, plus python/common for what more than one of them needs.
PACKAGES = {
    "common": "paths, media I/O, CSV tables, HTML pages",
    "fbx_tools": "the only place that imports the FBX SDK",
    "fbx_overlay": "FBX gridline metres, and the water-entry mesh overlays",
    "align": "camera-drift correction for the calibrated UVs",
    "stitch": "the six camera lines: pool, underwater, overhead, + 2 each",
    "dataset": "custody of inputs/, which git does not carry",
    "water_entry": "the single water-entry camera",
    "labeling": "browser labelers over the dataset snapshots",
    "keypoints": "COCO-17 annotation review pages",
    "benchmarks": "runtime metrics validation and summaries",
}

# Chains may lean on python/common freely; anything else crossing between them is
# listed here, so a new one is a decision rather than a drift. Each entry is a
# chain reaching for a PURE module (no FBX SDK, no OpenCV) that owns a rule it
# would otherwise copy.
CROSS_CHAIN_IMPORTS = {
    # underwater's reference textures come from the labeling dataset's snapshot
    # index; that line has no per-session clips to take a first frame from.
    ("stitch", "labeling"),
    # the overhead lines' gridlines ARE the calibration target, so extract writes
    # their metres into the one mesh.json using fbx_overlay's rules.
    ("stitch", "fbx_overlay"),
    # dataset asks both line registries which files a generation consists of, so
    # the manifest is derived rather than typed.
    ("dataset", "stitch"),
    ("dataset", "fbx_overlay"),
    # drift correction is one rule — register the new image against the one the
    # UVs were drawn on, then move the UVs — and both chains whose calibrations
    # drift need exactly it. A second copy would be two chances to get the
    # UV-origin flip backwards.
    ("stitch", "align"),
    ("fbx_overlay", "align"),
    # align's own entry point drives both chains to build the comparison matrix,
    # and scores a stitch line through that chain's own projection rather than a
    # second one of its own.
    ("align", "stitch"),
    ("align", "fbx_overlay"),
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
        # A package is a directory holding .py source. Retired packages leave a
        # __pycache__ full of .pyc behind (git does not remove empty directories,
        # and the bytecode is ignored), so a directory-name scan alone reports
        # long-deleted packages — python/underwater, python/assets and friends —
        # as if they were back.
        found = {path.name for path in (ROOT / "python").iterdir()
                 if path.is_dir() and not path.name.startswith("__")
                 and any(path.glob("*.py"))}
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

    def test_chains_cross_only_where_declared(self):
        """A chain importing another chain is the exception, not the pattern.

        python/common is shared by design and exempt; fbx_tools is the FBX SDK
        gate every chain that reads a model goes through. Everything else must be
        in CROSS_CHAIN_IMPORTS with a reason."""
        import re
        exempt = {"common", "fbx_tools"}
        found = set()
        for path in (ROOT / "python").rglob("*.py"):
            owner = path.relative_to(ROOT / "python").parts[0]
            if owner in exempt:
                continue
            text = path.read_text(encoding="utf-8")
            for other in re.findall(r"^\s*from python\.(\w+)", text, re.M) + \
                    re.findall(r"^\s*(?:import|from) python\.(\w+)", text, re.M):
                if other != owner and other not in exempt:
                    found.add((owner, other))
        self.assertEqual(found, CROSS_CHAIN_IMPORTS)

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
