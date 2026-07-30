"""One record per stitch line: everything that differs between them.

A stitch line is "N planes standing side by side across one lane": the meshes
sort left-to-right by world X, neighbours meet at a hard vertical seam, and the
world is already upright. The underwater 16-plane panorama and the overhead
2-plane lane are both instances; the six-camera pool is NOT — it sits in two
rows, is not ordered by world X, and blends by distance transform. Adding pool
here would grow fields that serve exactly one line, so it keeps its own path
(python.validation.reference_renderer + the CMake pool_4k.swasset rule).

Adding a third line means adding a record here and nothing else.
"""
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS = PROJECT_ROOT / "inputs"
OUTPUTS = PROJECT_ROOT / "outputs"
CONFIGS = INPUTS / "configs"
GENERATED = PROJECT_ROOT / "build" / "assets" / "generated"

_DEFAULT_DATASET = ("/Users/penghaotian/Downloads/DATAS/SWIMMING/"
                    "swimming-xlj-under-grids")


class StepError(RuntimeError):
    """A pipeline step failed; the message is already user-facing.

    Lives here rather than in run.py because clip lookup — the first thing that
    can fail for a caller-supplied directory — is a profile method, and run.py
    importing profiles is the right direction of dependency. run.py re-exports
    the name so its own callers and tests keep working.
    """


def grid_dir():
    """Where the underwater still renderer reads its grid textures.

    The canonical grid renders live in the dataset, not in all.fbm — the .fbm
    copies are stale. Overridable directly (STITCH_GRID_DIR) or via the dataset
    root the annotation_preview tools already use."""
    explicit = os.environ.get("STITCH_GRID_DIR")
    if explicit:
        return Path(explicit)
    dataset = os.environ.get("ANNOTATION_PREVIEW_DATASET_ROOT", _DEFAULT_DATASET)
    return Path(dataset) / "annotation-grids"


@dataclass(frozen=True)
class Profile:
    """The differences between one stitch line and another.

    Two pairs of fields look redundant but are not:

    `tex_dir` vs `still_tex_dir` — the first resolves texture basenames while
    reading the FBX (always the .fbm beside it); the second is what the still
    renderer actually reads. Underwater splits them (dataset grids beat the
    stale .fbm copies); overhead does not (the designer's calibration frames
    are in the .fbm).

    `ppm` vs `full_res` — ppm is the .swasset canvas density, which the runtime
    must honour exactly. full_res additionally rescales the *still* back to the
    source image height, so a human sees pixels at native scale. Underwater
    wants both (asset 6005x725, still 3278x360); overhead wants ppm only.
    """

    name: str
    fbx: Path
    tex_dir: Path
    still_tex_dir: Path
    camera_ids: tuple[str, ...]      # left-to-right, one per mesh in world-X order
    clip_suffix: str                 # ".ts" / ".mp4"
    ppm: float
    full_res: bool
    blend_px: float
    clip_uv: bool
    crop_bottom: str                 # "auto" | "none" | decimal string
    planes_only: bool
    sync: str                        # "manifest" | "none"
    source_size: tuple[int, int]
    ref_tex: str                     # "snapshot" | "video"
    out_dir: Path
    asset: Path

    @property
    def mesh_json(self):
        return self.out_dir / "mesh.json"

    @property
    def ref_tex_dir(self):
        return self.out_dir / "ref_tex"

    @property
    def metrics(self):
        return self.out_dir / "realtime.jsonl"

    def config_path(self, backend):
        return CONFIGS / f"{self.name}_{backend}.conf"

    def clip_for(self, video_dir, camera):
        """The one clip in `video_dir` belonging to `camera`.

        Both a missing and an ambiguous match are errors: silently picking one
        of two candidates would put the wrong camera on a plane, which shows up
        as a mis-registered seam far from here."""
        matches = sorted(Path(video_dir).glob(f"*_{camera}{self.clip_suffix}"))
        if not matches:
            raise StepError(
                f"no {self.clip_suffix} clip for {camera} in {video_dir}")
        if len(matches) > 1:
            raise StepError(f"ambiguous clips for {camera}: "
                            f"{[m.name for m in matches]}")
        return matches[0]


_UNDERWATER_MODELS = INPUTS / "underwater" / "models"
_OVERHEAD_MODELS = INPUTS / "overhead" / "models"

PROFILES = {
    "underwater": Profile(
        name="underwater",
        fbx=_UNDERWATER_MODELS / "all.fbx",
        tex_dir=_UNDERWATER_MODELS / "all.fbm",
        still_tex_dir=grid_dir(),
        camera_ids=tuple(f"underA{index}" for index in range(16, 0, -1)),
        clip_suffix=".ts",
        ppm=240.0,
        full_res=True,
        blend_px=120.0,
        clip_uv=True,
        crop_bottom="auto",
        planes_only=True,
        sync="manifest",
        source_size=(1280, 720),
        ref_tex="snapshot",
        out_dir=OUTPUTS / "underwater",
        asset=GENERATED / "underwater.swasset",
    ),
    "overhead": Profile(
        name="overhead",
        fbx=_OVERHEAD_MODELS / "002.fbx",
        tex_dir=_OVERHEAD_MODELS / "002.fbm",
        still_tex_dir=_OVERHEAD_MODELS / "002.fbm",
        camera_ids=("overhead5", "overhead6"),
        clip_suffix=".ts",
        # 170 sits just above the 152~169 px/m the source frames actually carry
        # (measured from the UV<->world affine), so nothing is upscaled.
        ppm=170.0,
        # ppm is already native, so there is nothing to rescale a still back to.
        full_res=False,
        # 85px @170ppm is 0.5m, the same physical width as underwater's 120px
        # @240ppm; the two planes overlap 425px so it fits.
        blend_px=85.0,
        clip_uv=True,
        # Both planes are full height: the measured ragged tail is 2 rows, which
        # is the renderer's own margin padding, not a perspective floor gap.
        crop_bottom="none",
        # 002.fbx carries exactly the two planes, no rigging or lane strips —
        # and the filter is not merely unnecessary here, it is wrong:
        # select_pool_planes keeps meshes whose world-Y sits inside the pool band
        # (-11.6, -8.0) where the underwater planes are, while this overhead
        # model spans Y [20.47, 23.47]. Turning it on drops both planes.
        planes_only=False,
        # These clips carry the same wall-clock manifest the underwater samples
        # do — align_start/align_end plus a per-lane keyframe anchor — and the
        # two lanes' keyframes land within 3ms of each other. Each overhead
        # sample also pairs with an underwater one recorded 1-2ms apart, which is
        # the point of the variant: the same swimmer from above and below.
        sync="manifest",
        source_size=(3840, 2160),
        ref_tex="video",
        out_dir=OUTPUTS / "overhead",
        asset=GENERATED / "overhead.swasset",
    ),
}


def names():
    return list(PROFILES)


def get(name):
    """The profile called `name`, or exit naming the ones that exist."""
    profile = PROFILES.get(name)
    if profile is None:
        raise SystemExit(
            f"unknown profile: {name}; valid: {', '.join(names())}")
    return profile
