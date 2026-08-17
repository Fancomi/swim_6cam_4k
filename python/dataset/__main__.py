"""Check inputs/ against the manifest, or rewrite the manifest from inputs/.

    python -m python.dataset                # both generations
    python -m python.dataset v2             # only the second
    python -m python.dataset --write        # regenerate docs/data-manifest.tsv

Three ways a hand-carried tree goes wrong, and each is reported as itself rather
than as one lump: a file is MISSING, its size differs (TRUNCATED — the usual
symptom of an interrupted copy), or its content differs at the same size (CONTENT
— the wrong revision of a texture, which is the dangerous one because everything
still runs and only the seams look off).

Files present on disk but absent from the manifest are reported as EXTRA and do
not fail the check: a stray export beside the real textures is untidy, not broken.
"""
import argparse
import sys

from python.common.paths import display
from python.dataset.manifest import (GENERATIONS, MANIFEST, dump, load, scan)


def compare(expected, actual):
    """(problems, extra) between two row lists, keyed by (line, path).

    `problems` is [(kind, gen, line, path, detail)]; `extra` is [(gen, line, path)].
    """
    want = {(row[1], row[4]): row for row in expected}
    have = {(row[1], row[4]): row for row in actual}
    problems = []
    for key, row in sorted(want.items()):
        gen, line, sha, size, path = row
        found = have.get(key)
        if found is None:
            problems.append(("MISSING", gen, line, path, "not on disk"))
        elif found[3] != size:
            problems.append(("TRUNCATED", gen, line, path,
                             f"{found[3]} bytes, expected {size}"))
        elif found[2] != sha:
            problems.append(("CONTENT", gen, line, path,
                             f"sha256 {found[2][:12]}…, expected {sha[:12]}…"))
    extra = [(row[0], row[1], row[4]) for key, row in sorted(have.items())
             if key not in want]
    return problems, extra


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m python.dataset", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("generation", nargs="?", choices=sorted(GENERATIONS),
                        help="check one generation only (default: all)")
    parser.add_argument("--write", action="store_true",
                        help="rewrite the manifest from what is on disk now; "
                             "only correct when this tree is known good")
    args = parser.parse_args(argv)

    rows = scan(args.generation)
    if args.write:
        if args.generation:
            parser.error("--write regenerates the whole manifest, so it cannot "
                         "be limited to one generation")
        MANIFEST.write_text(dump(rows), encoding="utf-8")
        total = sum(row[3] for row in rows) / 1048576
        print(f"wrote {display(MANIFEST)}: {len(rows)} files, {total:.1f} MB")
        return 0

    if not MANIFEST.is_file():
        print(f"error: manifest missing: {display(MANIFEST)}")
        return 2
    expected = load()
    if args.generation:
        expected = [row for row in expected if row[0] == args.generation]
    problems, extra = compare(expected, rows)

    for kind, gen, line, path, detail in problems:
        print(f"  {kind:<9} [{gen} {line}] {path} — {detail}")
    for gen, line, path in extra:
        print(f"  EXTRA     [{gen} {line}] {path} — not in the manifest")

    scope = args.generation or "+".join(GENERATIONS)
    checked = len(expected) - len(problems)
    total = sum(row[3] for row in rows) / 1048576
    if problems:
        print(f"\n{scope}: {checked}/{len(expected)} files OK, "
              f"{len(problems)} problems. See docs/DATA.md for where the data "
              f"lives.")
        return 1
    print(f"{scope}: {len(expected)} files OK ({total:.1f} MB)"
          + (f", {len(extra)} extra" if extra else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
