"""Extract 01d-style FBX into pool-compatible mesh JSON, ordered left-to-right.

Reuses python.assets.extract_fbx for all FBX/UV/geometry logic; this module only
adds underwater-specific defaults, left-to-right ordering, and an isolated CLI.
"""
import argparse
import json
import sys
from pathlib import Path

from python.assets import extract_fbx, fbx_common

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"


def _mesh_min_x(mesh):
    xs = [v["pos"][0] for tri in mesh["triangles"] for v in tri]
    return min(xs) if xs else float("inf")


def sort_meshes_by_world_x(meshes):
    """Return meshes ordered by each mesh's minimum world-X (pos[0]) ascending.

    Empty meshes (no triangles) sort last. Input list is not mutated."""
    return sorted(meshes, key=_mesh_min_x)
