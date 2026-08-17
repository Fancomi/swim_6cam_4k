"""One record per line.

A line is a set of camera(s) over one physical area, driven by one FBX set —
mirroring ``python/stitch/profiles.py`` where a rebuilt FBX of the same cameras
is a NEW line (``pool`` -> ``pool2``). Two lines exist:

    water_entry   legacy single-mesh FBX files (006.fbx/Plane004 vertical,
                  005.fbx/Plane005 surface) over background.jpg
    water_entry2  femto + gemini, each a per-camera FBX holding its full-frame
                  quad (the base image) + vertical + surface meshes

Only the water-entry cameras live here, because only they have a camera image to
draw the meshes on. The overhead planes are seen from straight above and are
stitch lines (``python -m python.stitch overhead2 extract,still``): their metres
go into that chain's ``outputs/<line>/mesh.json`` via `lane_meters`, using this
package's ``meters.annotate_meshes``. One document per line, not two.

Every difference between lines lives here; consumers never branch on a line
name. Adding a line means adding a record, nothing else.
"""
from dataclasses import dataclass
from pathlib import Path

from python.common.paths import INPUTS, OUTPUTS

from .classify import MeshKind  # noqa: F401  (re-exported for callers)


@dataclass(frozen=True)
class CameraSpec:
    """One sub-camera of a line: its FBX(es) and base-image source."""
    name: str
    fbxs: tuple[Path, ...]
    # Node whose texture IS the camera original (the full-frame quad). Old
    # models (005/006) have no quad — they use `base_image_path` instead.
    base_image_mesh: str | None = None
    # Explicit base image for models without a full-frame quad.
    base_image_path: Path | None = None
    # FBX UVs are bottom-origin (v=0 is the image bottom). A model authored
    # top-origin flips this per camera, not the renderer.
    v_origin: str = "bottom"

    @property
    def fbx(self):
        """The single FBX, or the first of several (for call sites that need one)."""
        return self.fbxs[0]


@dataclass(frozen=True)
class Profile:
    """One line and its sub-cameras."""
    name: str
    # One CameraSpec per sub-camera — a line may hold several FBX files (femto
    # and gemini are one line, two FBX).
    cameras: tuple[CameraSpec, ...]

    @property
    def out_dir(self):
        """Where this line's products land: ``outputs/<line>/overlay``.

        Under ``overlay/`` rather than ``outputs/<line>`` directly, to stay clear
        of the water-entry detection chain that owns the latter."""
        return OUTPUTS / self.name / "overlay"


_WATER_MODELS = INPUTS / "water_entry" / "models"
_WATER_BASE = INPUTS / "water_entry" / "background.jpg"


PROFILES = {
    # Legacy single-mesh water entry: two separate FBX files, one vertical
    # (006.fbx/Plane004) and one surface (005.fbx/Plane005), drawn over
    # background.jpg.
    "water_entry": Profile(
        name="water_entry",
        cameras=(
            CameraSpec(name="water_entry_a",
                       fbxs=(_WATER_MODELS / "006.fbx",),
                       base_image_mesh="Plane004",
                       base_image_path=_WATER_BASE),
            CameraSpec(name="water_entry_b",
                       fbxs=(_WATER_MODELS / "005.fbx",),
                       base_image_mesh="Plane005",
                       base_image_path=_WATER_BASE),
        ),
    ),
    # New per-camera water entry: femto and gemini, each FBX holding its own
    # full-frame quad (the camera image) + vertical + surface meshes.
    "water_entry2": Profile(
        name="water_entry2",
        cameras=(
            CameraSpec(name="femto",
                       fbxs=(_WATER_MODELS / "femto.fbx",),
                       base_image_mesh="Rectangle004"),
            CameraSpec(name="gemini",
                       fbxs=(_WATER_MODELS / "gemini.fbx",),
                       base_image_mesh="Rectangle005"),
        ),
    ),
}


def names():
    """Valid line names in PROFILES order."""
    return list(PROFILES)


def get(name):
    """Profile for `name`, listing the valid names on a miss (stitch style)."""
    try:
        return PROFILES[name]
    except KeyError:
        raise SystemExit(
            f"unknown line {name!r}; valid lines: {', '.join(PROFILES)}"
        ) from None


def camera_names():
    """Every sub-camera name across the lines (for the --camera shim)."""
    return [camera.name for profile in PROFILES.values()
            for camera in profile.cameras]


def line_for_camera(name):
    """The line owning sub-camera `name` (for the --camera compat shim)."""
    for line, profile in PROFILES.items():
        if any(camera.name == name for camera in profile.cameras):
            return line
    raise SystemExit(
        f"unknown camera {name!r}; valid cameras: {', '.join(camera_names())}"
    ) from None
