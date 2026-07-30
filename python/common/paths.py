"""Every path the repository derives from its own root, in one place.

Ten modules used to recompute the root with their own
`Path(__file__).resolve().parents[2]` or `os.path.dirname(...)` chain, in two
different styles. A single definition means moving a module one level deeper
cannot silently point it at the wrong tree.

External datasets are never hard-coded at a call site: each line names its own
environment variable through `dataset_root`, so pointing a machine at a
different copy is one export away.
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS = PROJECT_ROOT / "inputs"
OUTPUTS = PROJECT_ROOT / "outputs"
CONFIGS = INPUTS / "configs"
# Generated runtime assets live under build/: they are compiler output, not
# inputs, and CMake cleans the tree without touching anything hand-made.
GENERATED = PROJECT_ROOT / "build" / "assets" / "generated"


def dataset_root(variable, default):
    """The external dataset for one line: $variable, else `default`."""
    return Path(os.environ.get(variable) or default)


def display(path):
    """`path` relative to the repository root when it is inside it.

    Recorded in generated JSON so a mesh file says which model it came from
    without pinning one machine's absolute layout."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)
