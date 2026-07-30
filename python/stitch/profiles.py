"""One record per camera line. Every difference between lines lives here.

A line is "N planes covering one area, driven by N cameras". Three exist:

    pool        6 cameras over a 50m pool, two rows, distance-feathered
    underwater  16 planes down one lane, seen from below, hard vertical seams
    overhead    2 planes down the same lane, seen from above

They share every step — extract, still, video, asset, build, live — and differ
only in the fields below. Adding a fourth line means adding a record here and
nothing else; if it needs a new field, that field is the thing to think about.

Field pairs that look redundant but are not:

`tex_dir` vs `still_tex_dir` — the first resolves texture basenames while
reading the FBX (always the .fbm beside it); the second is what the still
renderer reads. Underwater splits them because the dataset's grid renders beat
the stale .fbm copies.

`ppm` vs `full_res` — ppm is the .swasset canvas density, which the runtime must
honour exactly. full_res additionally rescales the *still* back to source image
height so a human sees native pixels. Underwater wants both, pool and overhead
want ppm only.

`blend_px` vs `clip_uv` — pool's two rows overlap broadly at an angle, so it
feathers by distance (blend_px None) and must NOT clip at the image edge or the
feather gets cut. The plane lines meet at one vertical seam, so they blend across
a bounded band and clip, because mirrored out-of-range UVs would otherwise paint
a false strip exactly at the seam.
"""
from dataclasses import dataclass, field
import os
from pathlib import Path

from python.common.paths import (CONFIGS, GENERATED, INPUTS, OUTPUTS,
                                 PROJECT_ROOT, dataset_root)


class StepError(RuntimeError):
    """A step failed; the message is already user-facing.

    Lives here because clip lookup — the first thing that can fail on a
    caller-supplied directory — is a profile method."""


_UNDER_MODELS = INPUTS / "underwater" / "models"
_OVERHEAD_MODELS = INPUTS / "overhead" / "models"


def grid_dir():
    """Where the underwater still reads its grid textures.

    The canonical renders live in the dataset; the copies inside all.fbm are
    stale. STITCH_GRID_DIR overrides directly, otherwise it follows the dataset
    root the labeling tools already use.

    Read at call time, not at import: a caller that sets the variable after
    importing this module still gets the directory it asked for."""
    explicit = os.environ.get("STITCH_GRID_DIR")
    if explicit:
        return Path(explicit)
    return dataset_root(
        "SWIM_UNDER_GRIDS_ROOT",
        "/Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-xlj-under-grids",
    ) / "annotation-grids"


@dataclass(frozen=True)
class Profile:
    name: str
    fbx: Path
    tex_dir: Path
    camera_ids: tuple[str, ...]
    clip_suffix: str                     # ".ts" / ".mp4"
    ppm: float
    source_size: tuple[int, int]
    # None feathers by distance transform; a number is the vertical-seam band.
    blend_px: float | None = None
    clip_uv: bool = False
    neg_v: bool = False                  # flip world Y (the pool bake stores it down)
    order: str = "world_x"               # "world_x" | "declared"
    planes_only: bool = False            # drop clutter meshes (all.fbx)
    full_res: bool = False
    crop_bottom: str = "none"            # "auto" | "none" | decimal string
    # Padding, in pixels, added around the still's world bounds. A vertex sitting
    # exactly on the edge (or a hair past it after float rounding) otherwise
    # projects outside the raster and build_remap's slice shape stops matching.
    # Pool keeps 0: its published canvas is 5001x2101 and it has never rounded out.
    still_margin: int = 2
    sync: str = "none"                   # "manifest" | "none"
    ref_tex: str = "video"               # "snapshot" | "video"
    # Path, or a zero-argument callable resolved at use time (see
    # still_textures) for a directory that follows an environment variable.
    still_tex_dir: Path | None = None
    _out_dir: Path | None = field(default=None, repr=False)

    @property
    def out_dir(self):
        return self._out_dir or OUTPUTS / self.name

    @property
    def still_textures(self):
        """Where the still renderer reads its textures.

        A callable `still_tex_dir` is resolved here rather than at import, so an
        environment variable set after import still takes effect."""
        if self.still_tex_dir is None:
            return self.tex_dir
        return (self.still_tex_dir() if callable(self.still_tex_dir)
                else self.still_tex_dir)

    @property
    def mesh_json(self):
        return self.out_dir / "mesh.json"

    @property
    def ref_tex_dir(self):
        return self.out_dir / "ref_tex"

    @property
    def asset(self):
        return GENERATED / f"{self.name}.swasset"

    @property
    def metrics(self):
        return self.out_dir / "realtime.jsonl"

    @property
    def encode_path(self):
        return OUTPUTS / "videos" / f"{self.name}_realtime.h265"

    def config_path(self, backend):
        return CONFIGS / f"{self.name}_{backend}.conf"

    def clip_for(self, video_dir, camera):
        """The one clip in `video_dir` belonging to `camera`.

        Both a missing and an ambiguous match are errors: silently picking one of
        two candidates puts the wrong camera on a plane, which shows up as a
        mis-registered seam far away from here."""
        matches = sorted(Path(video_dir).glob(f"*_{camera}{self.clip_suffix}"))
        if not matches:
            raise StepError(
                f"no {self.clip_suffix} clip for {camera} in {video_dir}")
        if len(matches) > 1:
            raise StepError(f"ambiguous clips for {camera}: "
                            f"{[m.name for m in matches]}")
        return matches[0]


PROFILES = {
    "pool": Profile(
        name="pool",
        fbx=INPUTS / "pool" / "models" / "pool.fbx",
        tex_dir=INPUTS / "pool" / "textures",
        # Declared order, not world-X: the six meshes sit in TWO rows
        # (01/02/03 along one bank, u/Plane004/Plane007 along the other), so
        # sorting by X interleaves the banks and pairs each camera with the
        # opposite one's plane.
        camera_ids=("cam3", "cam2", "cam1", "cam4", "cam5", "cam6"),
        order="declared",
        clip_suffix=".mp4",
        ppm=100.0,
        source_size=(3840, 2160),
        # Feather, not a seam: the banks overlap broadly and meet at an angle, so
        # there is no single seam direction to pick. Where they do not overlap
        # (the pool centre line) the feather already leaves a clean hard cut.
        blend_px=None,
        clip_uv=False,
        neg_v=True,
        still_margin=0,
        # The recorder's manifest for these sessions carries no align window, so
        # there is no wall clock to align to; the clips are treated as t=0 aligned.
        sync="none",
        _out_dir=OUTPUTS / "pool",
    ),
    "underwater": Profile(
        name="underwater",
        fbx=_UNDER_MODELS / "all.fbx",
        tex_dir=_UNDER_MODELS / "all.fbm",
        still_tex_dir=grid_dir,
        camera_ids=tuple(f"underA{index}" for index in range(16, 0, -1)),
        clip_suffix=".ts",
        ppm=240.0,
        source_size=(1280, 720),
        blend_px=120.0,
        clip_uv=True,
        full_res=True,
        crop_bottom="auto",
        # all.fbx carries the 16 real planes plus untextured rigging, lane-marker
        # strips and duplicate copies.
        planes_only=True,
        sync="manifest",
        ref_tex="snapshot",
    ),
    "overhead": Profile(
        name="overhead",
        fbx=_OVERHEAD_MODELS / "002.fbx",
        tex_dir=_OVERHEAD_MODELS / "002.fbm",
        camera_ids=("overhead5", "overhead6"),
        clip_suffix=".ts",
        # 170 sits just above the 152~169 px/m the source frames actually carry
        # (measured from the UV<->world affine), so nothing is upscaled.
        ppm=170.0,
        source_size=(3840, 2160),
        # 85px @170ppm is 0.5m, the same physical width as underwater's 120px
        # @240ppm; the two planes overlap 425px so it fits.
        blend_px=85.0,
        clip_uv=True,
        # ppm is already native, so there is nothing to rescale a still back to,
        # and both planes are full height — the measured 2-row ragged tail is the
        # renderer's own margin padding, not a perspective floor gap.
        full_res=False,
        crop_bottom="none",
        # 002.fbx holds exactly the two planes, and the filter is not merely
        # unnecessary here but wrong: it keeps meshes inside the pool Y band
        # (-11.6, -8.0) where the underwater planes are, while this model spans
        # Y [20.47, 23.47] — turning it on drops both planes.
        planes_only=False,
        # These clips carry the same wall-clock manifest the underwater samples
        # do, and each overhead sample pairs with an underwater one recorded 1-2ms
        # apart: the same swimmer from above and below.
        sync="manifest",
        ref_tex="video",
    ),
}


def names():
    return list(PROFILES)


def get(name):
    """The profile called `name`, or exit naming the ones that exist."""
    profile = PROFILES.get(name)
    if profile is None:
        raise SystemExit(f"unknown line: {name}; valid: {', '.join(names())}")
    return profile


def default_video_dir(profile):
    """Where a line's clips live when --video-dir is not given.

    Only pool has one worth defaulting: its session directory is a machine-wide
    dataset, while the plane lines are per-sample directories chosen per run."""
    if profile.name != "pool":
        return None
    return dataset_root(
        "SWIMMING_DATASET_DIR",
        "/Users/penghaotian/Downloads/DATAS/SWIMMING/swim-6cam-4k/20260629-4K-raw",
    )


# Re-exported so callers do not need python.common.paths as well.
__all__ = ["PROFILES", "Profile", "StepError", "PROJECT_ROOT", "CONFIGS",
           "GENERATED", "OUTPUTS", "names", "get", "default_video_dir"]
