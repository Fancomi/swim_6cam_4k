"""Custody of the calibration inputs: the manifest, and what it catches."""
import tempfile
import unittest
from pathlib import Path

from python.dataset import manifest as M
from python.dataset.__main__ import compare, main


ROOT = Path(__file__).resolve().parents[2]


def _row(line, path, sha="a" * 64, size=100, gen="v1"):
    return (gen, line, sha, size, path)


class CompareTest(unittest.TestCase):
    """The three ways a hand-carried tree goes wrong, told apart.

    Told apart because they mean different things: a missing file is obvious once
    named, a size mismatch is an interrupted copy, and same-size-different-content
    is the wrong revision of a texture — the dangerous one, because everything
    still runs and only the seams look off.
    """

    def test_a_clean_tree_has_nothing_to_say(self):
        rows = [_row("pool", "pool/models/pool.fbx")]
        self.assertEqual(compare(rows, rows), ([], []))

    def test_a_missing_file_is_named(self):
        problems, extra = compare([_row("pool", "pool/models/pool.fbx")], [])
        self.assertEqual([(p[0], p[3]) for p in problems],
                         [("MISSING", "pool/models/pool.fbx")])
        self.assertEqual(extra, [])

    def test_a_short_file_reports_both_sizes(self):
        want = [_row("pool", "a.png", size=1000)]
        have = [_row("pool", "a.png", size=40)]
        problems, _extra = compare(want, have)
        self.assertEqual(problems[0][0], "TRUNCATED")
        self.assertIn("40", problems[0][4])
        self.assertIn("1000", problems[0][4])

    def test_same_size_different_content_is_its_own_kind(self):
        want = [_row("pool", "a.png", sha="a" * 64)]
        have = [_row("pool", "a.png", sha="b" * 64)]
        problems, _extra = compare(want, have)
        self.assertEqual(problems[0][0], "CONTENT")

    def test_an_unexpected_file_is_reported_but_not_a_problem(self):
        problems, extra = compare([], [_row("pool", "pool/textures/stray.png")])
        self.assertEqual(problems, [])
        self.assertEqual(extra, [("v1", "pool", "pool/textures/stray.png")])

    def test_the_same_path_under_two_lines_is_tracked_per_line(self):
        """The lane schematic belongs to both overhead generations, so the key is
        (line, path) — keying on path alone would hide one line's copy."""
        schematic = "overhead/ label_line.png"
        want = [_row("overhead", schematic), _row("overhead2", schematic, gen="v2")]
        have = [_row("overhead", schematic)]
        problems, _extra = compare(want, have)
        self.assertEqual([(p[0], p[2]) for p in problems],
                         [("MISSING", "overhead2")])


class ManifestTest(unittest.TestCase):
    def test_round_trip_through_the_file_format(self):
        rows = [("v1", "pool", "a" * 64, 123, "pool/models/pool.fbx"),
                ("v2", "overhead2", "b" * 64, 456, "overhead/25 水面.fbx")]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.tsv"
            path.write_text(M.dump(rows), encoding="utf-8")
            self.assertEqual(M.load(path), rows)

    def test_comments_and_blank_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "m.tsv"
            path.write_text("# a note\n\nv1\tpool\t%s\t7\tp.fbx\n" % ("c" * 64),
                            encoding="utf-8")
            self.assertEqual(M.load(path),
                             [("v1", "pool", "c" * 64, 7, "p.fbx")])

    def test_every_line_belongs_to_exactly_one_generation(self):
        """A line is v1 or v2, never both and never neither — that is what makes
        `check_inputs.sh v2` able to scope the check to what you actually carried."""
        from python.fbx_overlay import profiles as overlay
        from python.stitch import profiles as stitch
        listed = [line for lines in M.GENERATIONS.values() for line in lines]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(set(listed),
                         set(stitch.names()) | set(overlay.names()))

    def test_paths_come_from_the_profiles(self):
        """Not typed into the manifest: a line's paths are its profile's fields,
        so adding a line needs no edit here."""
        from python.stitch import profiles as stitch
        pool = stitch.get("pool")
        self.assertIn(pool.fbx, M.line_paths("pool"))
        self.assertIn(pool.tex_dir, M.line_paths("pool"))
        overhead = stitch.get("overhead2")
        self.assertIn(overhead.label_line, M.line_paths("overhead2"))
        # An overlay line contributes each sub-camera's FBX and its .fbm
        paths = M.line_paths("water_entry2")
        self.assertTrue(any(p.name == "femto.fbx" for p in paths))
        self.assertTrue(any(p.name == "femto.fbm" for p in paths))


@unittest.skipUnless((ROOT / "docs" / "data-manifest.tsv").is_file(),
                     "the manifest is not present")
class CommittedManifestTest(unittest.TestCase):
    def test_it_parses_and_covers_both_generations(self):
        rows = M.load()
        self.assertTrue(rows)
        self.assertEqual({row[0] for row in rows}, set(M.GENERATIONS))
        for _gen, _line, sha, size, path in rows:
            self.assertEqual(len(sha), 64, path)
            self.assertGreater(size, 0, path)

    def test_every_line_appears(self):
        listed = {row[1] for row in M.load()}
        expected = {line for lines in M.GENERATIONS.values() for line in lines}
        self.assertEqual(listed, expected)


@unittest.skipUnless((ROOT / "inputs" / "pool" / "models" / "pool.fbx").is_file(),
                     "the calibration inputs are not on this machine")
class LiveTreeTest(unittest.TestCase):
    """End to end against the real tree: the manifest must describe what is here.

    This is the test that would have caught a manifest generated from a different
    machine's copy, and the one that fails loudly when a file is carried in wrong.
    """

    def test_the_tree_matches_the_manifest(self):
        problems, _extra = compare(M.load(), M.scan())
        self.assertEqual(problems, [], "\n".join(
            f"{kind} [{gen} {line}] {path}: {detail}"
            for kind, gen, line, path, detail in problems))

    def test_damage_is_classified_by_kind(self):
        """Real files, real digests, three real kinds of damage."""
        expected = M.load()
        actual = {(row[1], row[4]): row for row in M.scan()}

        missing = next(k for k in actual if k[0] == "pool")
        broken = dict(actual)
        del broken[missing]
        short_key = next(k for k in actual if k[0] == "underwater")
        gen, line, sha, size, path = broken[short_key]
        broken[short_key] = (gen, line, sha, size - 1, path)
        wrong_key = next(k for k in actual if k[0] == "overhead")
        gen, line, sha, size, path = broken[wrong_key]
        broken[wrong_key] = (gen, line, "f" * 64, size, path)

        problems, _extra = compare(expected, list(broken.values()))
        kinds = {(p[0], (p[2], p[3])) for p in problems}
        self.assertIn(("MISSING", missing), kinds)
        self.assertIn(("TRUNCATED", short_key), kinds)
        self.assertIn(("CONTENT", wrong_key), kinds)
        self.assertEqual(len(problems), 3)


if __name__ == "__main__":
    unittest.main()
