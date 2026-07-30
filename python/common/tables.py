"""CSV tables with the boilerplate in one place.

Thirteen call sites each wrote `os.makedirs(dirname, exist_ok=True)`,
`open(..., newline="")`, `writeheader()`, `writerows()` by hand. The
`newline=""` is not optional: without it csv writes \r\r\n on Windows and every
row gains a blank line.
"""
import csv
from pathlib import Path


def read_rows(path):
    """Every row of `path` as a dict keyed by its header."""
    with open(str(path), newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_rows(path, columns, rows, drop_extra=False):
    """Write `rows` (dicts) under `columns`, creating parent directories.

    `drop_extra` allows rows to carry keys that are not columns, for callers that
    keep diagnostic fields on the row and publish only a subset. It defaults off
    so a mistyped column name surfaces as an error instead of a missing column."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns),
                                extrasaction="ignore" if drop_extra else "raise")
        writer.writeheader()
        writer.writerows(rows)
    return path
