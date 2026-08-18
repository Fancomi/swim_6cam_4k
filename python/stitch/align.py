"""Resolve one stitch line's drift correction: which reference, which probe.

The align chain is generic — two images in, a transform out. This is the stitch
line's half of the contract: for each camera, WHICH image was the calibration
made against, and where does the new data come from.

The reference is each mesh's OWN baked texture (`tex_dir / texture_basename`),
not a same-named image from the dataset, and that is not interchangeable.
underwater2's A10 is calibrated against `10.png` — the A10 clip's first frame from
sample swb_20260813-170549_24, complete with a swimmer, because that is what the
designer re-registered its UVs on. Substituting the clean `underA10_background.png`
made A10's two seams worse and dropped the whole line's mean seam gain from +0.039
to +0.001. The rule is simply: register against the image the UVs were drawn on,
whatever is in it.
"""
from pathlib import Path

from python.align import cache as align_cache
from python.align.aligner import DEFAULT_MODEL
from python.align.probe import probe_cameras
from python.common.media import MediaError, read_image
from python.stitch import compose as C


def reference_textures(profile, meshes=None):
    """{camera: the image this camera's UVs were calibrated against}.

    Read from the profile's `tex_dir` — the .fbm beside the FBX, which is what
    `extract` resolved the basenames against — rather than `still_textures`,
    which a line may point elsewhere for rendering (underwater's grid renders)."""
    if meshes is None:
        meshes = C.load_meshes(profile.mesh_json, neg_v=profile.neg_v,
                               neg_u=profile.neg_u)
    references = {}
    for camera, mesh in zip(profile.camera_ids, meshes):
        basename = mesh.get("texture_basename")
        if not basename:
            continue
        try:
            references[camera] = read_image(profile.tex_dir / basename,
                                           f"calibration texture for {camera}")
        except MediaError as error:
            print(f"  {camera:12s} (no calibration texture: {error})")
    return references


def cache_path(profile, key):
    """Where a line's solved alignments live: outputs/<line>/align/<key>/align.json.

    Keyed by the dataset it was solved against, so the 202607 and 202608 answers
    for the same line coexist instead of overwriting each other."""
    return profile.out_dir / "align" / key / "align.json"


def default_key(probe_dir):
    """A cache key naming the dataset directory, e.g. `202608_swb_...`.

    Two components because the sample directories are only unique within their
    month directory, and a bare sample name would collide across datasets."""
    path = Path(probe_dir).resolve()
    parent = path.parent.name
    return f"{parent}_{path.name}" if parent else path.name


def resolve(profile, probe_dir, model=DEFAULT_MODEL, key=None, use_cache=True,
            force=False, pattern=None, meshes=None):
    """Alignments in profile camera order, ready for `render(alignments=...)`.

    Returns (alignments, probes, key): the probes come back because the caller
    usually wants to stitch the very images that were registered — that is what
    makes the before/after comparison honest, both halves seeing identical
    pixels and differing only in the UVs."""
    key = key or default_key(probe_dir)
    references = reference_textures(profile, meshes)
    print(f"probing {probe_dir}")
    probes = probe_cameras(probe_dir, profile.camera_ids, pattern)
    path = cache_path(profile, key) if use_cache else None
    solved = align_cache.resolve(profile.name, profile.camera_ids, references,
                                probes, model=model, cache_path=path,
                                force=force, report=print)
    for camera in profile.camera_ids:
        alignment = solved.get(camera)
        if alignment is None:
            continue
        mark = "ok " if alignment.accepted else "SKIP"
        print(f"  {mark} {camera:12s} d=({alignment.shift_px[0]:+7.2f},"
              f"{alignment.shift_px[1]:+6.2f})px rot={alignment.rotation_deg:+5.2f} "
              f"ncc {alignment.ncc_before:.3f}->{alignment.ncc_after:.3f}"
              + (f"  ({alignment.reason})" if alignment.reason else ""))
    return [solved.get(camera) for camera in profile.camera_ids], probes, key
