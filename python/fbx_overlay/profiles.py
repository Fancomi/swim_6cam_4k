"""One record per line.

A line is a set of camera(s) over one physical area, driven by one FBX set —
mirroring ``python/stitch/profiles.py`` where a rebuilt FBX of the same cameras
is a NEW line (``pool`` -> ``pool2``, ``underwater`` -> ``underwater2``). Here
the four lines are:

    water_entry   legacy single-mesh FBX files (006.fbx/Plane004 vertical,
                  005.fbx/Plane005 surface) over background.jpg
    water_entry2  femto + gemini, each a per-camera FBX holding its full-frame
                  quad (the base image) + vertical + surface meshes
    overhead      old 002.fbx, two planes seen from above (mirrors the stitch
                  overhead line)
    overhead2     new 25 水面.fbx, two rebuilt planes (today's fbx_overlay
                  overhead, renamed)

Two modes:
- ``base_image``: each CameraSpec's FBX carries a full-frame quad whose texture
  IS the camera image; the overlay draws on that image.
- ``canvas``: one FBX holds all planes; the renderer stitches them onto a
  world-projected canvas (the stitch chain's Canvas) and overlays grid + meters.

Every difference between lines lives here; consumers never branch on a line
name. Adding a line means adding a record, nothing else.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path

from python.common.paths import INPUTS, OUTPUTS, dataset_root

from .classify import MeshKind  # noqa: F401  (re-exported for callers)


@dataclass(frozen=True)
class CameraSpec:
    """One sub-camera of a base_image line: its FBX(es) and base-image source."""
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
    """One line. `mode` selects the renderer; the rest is that line's data."""
    name: str                    # the line name
    mode: str = "base_image"     # "base_image" | "canvas"
    # base_image mode: one CameraSpec per sub-camera (a line may hold several
    # FBX files — femto and gemini are one line but two FBX).
    cameras: tuple[CameraSpec, ...] = ()
    # canvas mode: a single FBX holding all planes.
    fbx: Path | None = None
    # (name, dir, (texture basenames...)) — alternate texture sources for the
    # canvas renderer.
    texture_sets: tuple = ()
    tex_dir: Path | None = None
    camera_ids: tuple = ()
    ppm: float = 0.0
    blend_px: float | None = None
    clip_uv: bool = False
    still_margin: int = 2
    # A lane-schematic reference image to compare the overlay against.
    label_line: Path | None = None
    _out_dir: Path | None = field(default=None, repr=False)

    @property
    def out_dir(self):
        """Where this line's overlays land (default outputs/<line>)."""
        return self._out_dir or OUTPUTS / self.name


_WATER_MODELS = INPUTS / "water_entry" / "models"
_OVERHEAD_MODELS = INPUTS / "overhead" / "models"
_WATER_BASE = INPUTS / "water_entry" / "background.jpg"


def overhead_frames_dir():
    """The dataset texture set for the overhead planes.

    Read at call time, not at import, so a caller that sets the environment
    variable after importing still gets the directory it asked for (mirrors
    ``python.stitch.profiles.grid_dir``).
    """
    return dataset_root(
        "OVERHEAD_OBJECT_FRAMES_ROOT",
        "/Users/penghaotian/Downloads/DATAS/SWIMMING/"
        "swimming-xlj-under-grids/20260708/object-frames",
    )


PROFILES = {
    # Legacy single-mesh water entry: two separate FBX files, one vertical
    # (006.fbx/Plane004) and one surface (005.fbx/Plane005), drawn over
    # background.jpg. Originally hardcoded defaults; now a real line.
    "water_entry": Profile(
        name="water_entry",
        mode="base_image",
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
        camera_ids=("water_entry_a", "water_entry_b"),
        _out_dir=OUTPUTS / "water_entry",
    ),
    # New per-camera water entry: femto and gemini, each FBX holding its own
    # full-frame quad (the camera image) + vertical + surface meshes.
    "water_entry2": Profile(
        name="water_entry2",
        mode="base_image",
        cameras=(
            CameraSpec(name="femto",
                       fbxs=(_WATER_MODELS / "femto.fbx",),
                       base_image_mesh="Rectangle004"),
            CameraSpec(name="gemini",
                       fbxs=(_WATER_MODELS / "gemini.fbx",),
                       base_image_mesh="Rectangle005"),
        ),
        camera_ids=("femto", "gemini"),
        _out_dir=OUTPUTS / "water_entry2",
    ),
    # Old overhead line (002.fbx), mirroring python.stitch.profiles "overhead"
    # so the overlay and the stitch output share the same world->pixel
    # projection. Products go to outputs/overhead/overlay/ — the stitch line
    # owns outputs/overhead/mesh.json.
    "overhead": Profile(
        name="overhead",
        mode="canvas",
        fbx=_OVERHEAD_MODELS / "002.fbx",
        tex_dir=_OVERHEAD_MODELS / "002.fbm",
        texture_sets=(
            ("fbx", _OVERHEAD_MODELS / "002.fbm",
             ("C06.jpg", "05-02.jpg")),
        ),
        camera_ids=("overhead5", "overhead6"),
        ppm=170.0,
        blend_px=85.0,
        clip_uv=True,
        still_margin=2,
        label_line=INPUTS / "overhead" / " label_line.png",
        _out_dir=OUTPUTS / "overhead" / "overlay",
    ),
    # New overhead line (25 水面.fbx) — the rebuilt planes.
    "overhead2": Profile(
        name="overhead2",
        mode="canvas",
        fbx=_OVERHEAD_MODELS / "25 水面.fbx",
        tex_dir=_OVERHEAD_MODELS / "25 水面.fbm",
        texture_sets=(
            ("fbx", _OVERHEAD_MODELS / "25 水面.fbm",
             ("overhead5_merged.png", "overhead6_merged 拷贝.png")),
            ("dataset", overhead_frames_dir(),
             ("overhead5_merged.png", "overhead6_merged.png")),
        ),
        camera_ids=("overhead5", "overhead6"),
        ppm=170.0,
        blend_px=85.0,
        clip_uv=True,
        still_margin=2,
        label_line=INPUTS / "overhead" / " label_line.png",
        _out_dir=OUTPUTS / "overhead2",
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
    """Every sub-camera name across base_image lines (for --camera choices)."""
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
