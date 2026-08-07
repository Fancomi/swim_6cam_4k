"""Dataset snapshot index: where a camera's frames live on disk.

The underwater dataset ships synchronised snapshots, each a directory of one
jpg per camera. Two very different consumers read that index — the overhead
temporal merge and the stitch line's reference-texture export — so it lives here
rather than in either of them.

Two layouts coexist. The newer dataset root is organised by capture date, so the
snapshot directories sit at `<root>/<date>/snapshots/` and a caller passes that
date in; the older layout keeps them directly at `<root>/snapshots/`, which stays
the default when `date` is not given.
"""
import glob
import os
from pathlib import Path

from python.common.paths import OUTPUTS, dataset_root

DATASET = dataset_root(
    "SWIM_UNDER_GRIDS_ROOT",
    "/Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-xlj-under-grids",
)
SNAPSHOTS = DATASET / "snapshots"
OUTPUT_ROOT = dataset_root("SWIM_LABELING_OUTPUT_ROOT", OUTPUTS / "labeling")

# RGB Euclidean distance above which a pixel counts as changed against the
# median background. 40 was picked by sweeping the per-frame change fraction
# across all 16 cameras: below it the water surface ripple registers as
# foreground, above it a dark wetsuit against dark water does not.
DIST_THRESH = 40


def snapshot_dirs(date=None):
    """Sorted `raw_*` snapshot directories, in time order (dir name sorts chronologically).

    `date` selects the dated layout `<root>/<date>/snapshots`; without it the
    legacy `<root>/snapshots` layout is used."""
    base = SNAPSHOTS if date is None else DATASET / str(date) / "snapshots"
    return sorted(glob.glob(os.path.join(str(base), "raw_*")))


def frames_for_camera(camera, date=None):
    """[(snapshot_id, path)] for one camera across every snapshot, in time order."""
    found = []
    for directory in snapshot_dirs(date):
        hits = glob.glob(os.path.join(directory, f"*__{camera}.jpg"))
        if hits:
            found.append((os.path.basename(directory), hits[0]))
    return found
