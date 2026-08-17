"""One record per camera line. Every difference between lines lives here.

A line is "N planes covering one area, driven by N cameras". Six exist, covering
three physical areas — each of them twice, because a rebuilt FBX of the same
cameras is a new line, not a new step:

    pool         6 cameras over a 50m pool, two rows, distance-feathered
    pool2        the same 6, from a hand-rebuilt FBX modelled 180° round
    underwater   16 planes down one lane, seen from below, hard vertical seams
    underwater2  the same 16, re-surveyed against a changed calibration target
    overhead     2 planes down the same lane, seen from above
    overhead2    the same 2, rebuilt with the 2.5m lane's middle line

They share every step — extract, still, video, asset, build, live — and differ
only in the fields below. Adding a seventh line means adding a record here and
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


_POOL_MODELS = INPUTS / "pool" / "models"
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
    # Flip world X. With neg_v it makes a 180°-rotated file land on the shared
    # axes, so every step (still, video, asset) reads geometry that is already
    # aligned — the rotation stays one profile field, not a step each consumer
    # would have to repeat.
    neg_u: bool = False
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
    # A lane schematic carrying the designer's distance marks. The still stamps
    # its ink on the composite as a fifth product, which is the only way to check
    # the declared metres against the photographed lane. None for lines that
    # have no schematic.
    label_line: Path | None = None
    # Annotate the mesh JSON's gridlines with real-world metres (see
    # python/fbx_overlay/meters.py). On for the overhead lines, whose planes ARE
    # the calibration target the algorithm side measures against; off for the
    # rest, whose meshes carry no agreed distance marks and whose JSON the
    # runtime asset is compiled from byte-for-byte.
    lane_meters: bool = False
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
    # Same six cameras and the same clips as `pool`, but the designer rebuilt the
    # meshes by hand (2.5m lanes gained two more strung lines, so the grid is
    # denser: 442~494 triangles against the old 160~190). Three things differ and
    # all three are profile fields — nothing in python/stitch/ branches on a line
    # name, and no consumer post-processes the geometry:
    #
    #   const_axis        old file is flat in Y (pos = X,Z); this one is flat in Z
    #                     (pos = X,Y). extract_mesh picks that up on its own.
    #   neg_u + neg_v     the file models the pool rotated 180° from pool.fbx: the
    #                     same camera sits on the opposite bank and the opposite
    #                     end (cam1 is bottom-right here, top-left there). Setting
    #                     BOTH mirrors is that rotation, so the mesh arrives on
    #                     pool's axes and every step downstream — still, video,
    #                     asset — sees an already-aligned pool. Verified by
    #                     normalising each camera's centre into canvas fractions:
    #                     with both flips the worst deviation from pool is 0.020
    #                     (one flip alone leaves 0.50~0.67, none leaves 0.84).
    #   camera_ids        comes from each mesh's OWN texture, not from where the
    #                     mesh sits. The .fbm filenames are recycled from another
    #                     shoot (overhead5_merged.png and friends) but the pixels
    #                     are these six pool cameras; correlating each texture
    #                     against the clips' first frames identifies them
    #                     unambiguously (0.67~0.86, next-best 0.45~0.68). Guessing
    #                     from world position instead gets all six wrong because
    #                     of that same 180°, and the stitch then looks shattered
    #                     while the UVs are in fact fine.
    "pool2": Profile(
        name="pool2",
        fbx=_POOL_MODELS / "pool 1.fbx",
        tex_dir=_POOL_MODELS / "pool 1.fbm",
        camera_ids=("cam5", "cam6", "cam4", "cam1", "cam3", "cam2"),
        order="declared",
        clip_suffix=".mp4",
        ppm=100.0,
        source_size=(3840, 2160),
        blend_px=None,
        clip_uv=False,
        neg_v=True,
        neg_u=True,
        still_margin=0,
        sync="none",
        _out_dir=OUTPUTS / "pool2",
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
    # Same 16 cameras and the same clips as `underwater`, re-surveyed against a
    # changed calibration target and rebuilt by hand. Three fields differ and every
    # one of them is a property of the file, not a step:
    #
    #   planes_only   OFF, and that is not merely unnecessary — it is required.
    #                 all.fbx needs the Y-band filter to reject rigging and
    #                 duplicate copies; this file holds exactly 16 nodes, one per
    #                 texture, and its planes span Y [-10.33, -7.58], well above
    #                 select_planes' hardcoded (-11.6, -8.0) band. Turn it on and
    #                 all 16 are dropped ("no pool plane found").
    #   still_tex_dir NOT split. underwater points the still at the dataset's
    #                 annotation-grids because the copies in all.fbm are stale;
    #                 here the .fbm holds the delivered mask composites verbatim
    #                 (byte-identical to 20260807/object-frames/), so the .fbm IS
    #                 canonical and one directory serves both readers.
    #   ref_tex       "video", not "snapshot": the snapshots moved under a date
    #                 layer, so frames_for_camera() finds nothing for a bare
    #                 camera id and `tex` would export an empty directory.
    #
    # Everything else is deliberately identical to underwater, which is what makes
    # the two lines comparable frame for frame. The planes are narrower here
    # (1.5~3.5m against 3.0~5.5m), so neighbours overlap 0.50~1.00m where they used
    # to overlap 1.5~4.5m. blend_px stays 120 because it is a physical width —
    # 120px @240ppm is 0.5m — so the tightest pair is now exactly one full
    # crossfade rather than a narrow band inside a wide overlap.
    #
    # The FBX is on its fourth revision (`8.15.fbx` replaced
    # `ALL OK- 8.14-02.fbx`). 8.14-02 was not "only vertices moved": it dropped
    # A1 entirely and re-baked the textures. The mesh nodes number 02..16,
    # texture names are `underA{2..16}_background.png` (the dataset's bare
    # backgrounds, byte-identical to 20260807/object-frames/, where the earlier
    # revision used the mask composites `underA*_mask_merged.png`). So
    # camera_ids loses underA1 and every per-mesh texture changes. Geometry
    # also moved: planes now span Y [-10.092, -7.342] (0.25m lower than the 8.14
    # revision's [-10.332, -7.582]) and the X extents differ from the old file —
    # A2 reaches 62.955 where it used to stop at 61.955, so the rightmost plane
    # now overlaps the lane differently. Orientation still needs no mirror: A16
    # sits at min world X and the vertical axis rises, the same way round as
    # all.fbx.
    #
    # 8.15 keeps every vertex position (same 15 nodes, same X spans, same Y band
    # [-10.092, -7.342]) and changes A10 alone — the node at A10's world X
    # [47.955, 49.955], named `017`. Two things about it moved:
    #
    #   its UVs      re-registered ~5px down on a 130px/m canvas (3.8cm). Proved
    #                by rendering both revisions against the SAME dataset
    #                backgrounds: every other plane matches bit for bit
    #                (corr 1.0000, dy 0), A10 needs dy=+5 to line up. Since the
    #                texture is held constant there, only the UVs can explain it.
    #   its texture  `10.png` instead of `underA10_background.png` — and that file
    #                is the A10 clip's first frame from sample
    #                swb_20260813-170549_24 (byte-identical to what `tex` exports
    #                for that sample), not a clean median background. So a `still`
    #                off the .fbm shows one plane carrying that frame's swimmer
    #                and splash while the other 14 stay clean. The other 14 .fbm
    #                textures are still byte-identical to 20260807/object-frames/.
    #
    # The seams either side of A10 stay registered (dy 0, corr 0.96~0.97 against
    # 0.93~0.95 before), so the re-registration improved A10 rather than skewing
    # it. Camera identity is positional, so camera_ids is unchanged.
    #
    # 13 of 14 neighbour overlaps are exactly 0.50m, so blend_px stays 120
    # (a physical width: 120px @240ppm is 0.5m, a full crossfade at the tightest
    # pair and bounded at the widest).
    "underwater2": Profile(
        name="underwater2",
        fbx=_UNDER_MODELS / "8.15.fbx",
        tex_dir=_UNDER_MODELS / "8.15.fbm",
        camera_ids=tuple(f"underA{index}" for index in range(16, 1, -1)),
        clip_suffix=".ts",
        ppm=240.0,
        source_size=(1280, 720),
        blend_px=120.0,
        clip_uv=True,
        full_res=True,
        crop_bottom="auto",
        planes_only=False,
        sync="manifest",
        ref_tex="video",
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
        label_line=INPUTS / "overhead" / " label_line.png",
        lane_meters=True,
    ),
    # Same two cameras and the same clips as `overhead`, rebuilt by hand: the
    # 2.5m lane gained a middle strung line (200/340 triangles against the old
    # 120/204) and the model moved up in world Y ([31.94, 34.94] against
    # [20.47, 23.47]) — a translation, so nothing downstream changes. ppm,
    # blend_px and clip_uv are identical, which is what makes the two lines
    # comparable: both land on the same 4255x515 canvas.
    #
    # Camera identity verified by pixel correlation against the clips' first
    # frames rather than by filename: overhead5_merged.png -> overhead5 (0.858
    # vs 0.372 next-best), `overhead6_merged 拷贝.png` -> overhead6 (0.843 vs
    # 0.377). Positional order after the world-X sort agrees, so camera_ids is
    # unchanged.
    "overhead2": Profile(
        name="overhead2",
        fbx=_OVERHEAD_MODELS / "25 水面.fbx",
        tex_dir=_OVERHEAD_MODELS / "25 水面.fbm",
        camera_ids=("overhead5", "overhead6"),
        clip_suffix=".ts",
        ppm=170.0,
        source_size=(3840, 2160),
        blend_px=85.0,
        clip_uv=True,
        full_res=False,
        crop_bottom="none",
        planes_only=False,
        sync="manifest",
        ref_tex="video",
        label_line=INPUTS / "overhead" / " label_line.png",
        lane_meters=True,
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

    Only the pool lines have one worth defaulting: their session directory is a
    machine-wide dataset, while the plane lines are per-sample directories chosen
    per run. `pool2` is the same six cameras filming the same session as `pool`
    (only the FBX was rebuilt), so it defaults to the same clips — that is what
    makes the two lines comparable frame for frame."""
    if profile.name not in ("pool", "pool2"):
        return None
    return dataset_root(
        "SWIMMING_DATASET_DIR",
        "/Users/penghaotian/Downloads/DATAS/SWIMMING/swim-6cam-4k/20260629-4K-raw",
    )


# Re-exported so callers do not need python.common.paths as well.
__all__ = ["PROFILES", "Profile", "StepError", "PROJECT_ROOT", "CONFIGS",
           "GENERATED", "OUTPUTS", "names", "get", "default_video_dir"]
