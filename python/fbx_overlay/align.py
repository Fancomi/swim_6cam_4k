"""Resolve one water-entry camera's drift correction.

The overlay chain's half of the align contract. Simpler than the stitch line's:
one camera, one new image, given explicitly as `--align-to`. There is no clip
directory to search because a water-entry camera's "new data" in this workflow is
a single frame someone hands over.

The reference is the camera's own base image — for femto/gemini the full-frame
quad's texture, which IS the frame the UVs were drawn on. That is the same rule
the stitch side follows, arrived at from the other direction: this chain already
extracts the base image from the FBX for drawing, so the image that must be
registered against is the one already in hand.

The measured case: femto's calibration texture is from the 20260708 era, and its
UVs land ~50px (mean, over 420 vertices) away from where the 20260807 image puts
the same pool features. `water_entry2/femto` is the same geometry hand-recalibrated
on 20260807 — identical vertex positions, only the UVs differ — so it is ground
truth for this correction. Aligning brings the error to ~17px.
"""
from python.align import cache as align_cache
from python.align.aligner import DEFAULT_MODEL
from python.align.probe import probe
from python.common.media import read_image

from .classify import MeshKind
from .render import OverlayError, load_texture


def calibration_image(camera, meshes):
    """The image this camera's UVs were drawn on.

    Three sources, in the order that makes them authoritative:

    - `base_image_path`, when the line declares one. The legacy 005/006 models
      carry no full-frame quad, and their per-mesh textures are NOT the frame the
      UVs were registered against — 006.fbm's texture correlates -0.10 against
      background.jpg, while background.jpg is byte-for-byte the image femto's own
      model uses. Registering against the mesh texture makes ECC fail outright,
      which is how this was found.
    - the full-frame quad's texture, for the femto/gemini models, whose quad IS
      the camera frame.
    - failing both, the first drawn mesh's texture, which is at least an image
      from this camera.

    Also the answer to "what does the overlay draw on", which is why
    ``__main__._resolve_base_image`` defers to it: the frame the UVs were drawn on
    and the frame they should be drawn over are the same frame, and two functions
    disagreeing about that would put the mesh on one image and align it to another.
    """
    if getattr(camera, "base_image_path", None) is not None:
        return read_image(camera.base_image_path, "base image")
    quads = [mesh for mesh in meshes if mesh.get("kind") is MeshKind.FULL_FRAME]
    if quads:
        return load_texture(quads[0], "base image")
    drawn = [mesh for mesh in meshes if mesh.get("kind") is not MeshKind.FULL_FRAME]
    if not drawn:
        raise OverlayError(f"no mesh in {camera.fbx} carries a camera image")
    return load_texture(drawn[0], "base image")


def parse_targets(values, cameras):
    """`--align-to` values to {camera: path}.

    Accepts `CAMERA=PATH` for a line with several cameras (femto and gemini are
    one line, one image each) and a bare `PATH` for a line with one — or, when
    repeated bare, applies to every camera in turn. A bare path with several
    cameras is an error rather than a guess: registering gemini against femto's
    frame yields a confident, wrong transform."""
    targets = {}
    for value in values:
        camera, separator, path = value.partition("=")
        if separator:
            if camera not in cameras:
                raise ValueError(f"unknown camera {camera!r} in --align-to; "
                                 f"this line has: {', '.join(cameras)}")
            targets[camera] = path
        elif len(cameras) == 1:
            targets[cameras[0]] = value
        else:
            raise ValueError(
                f"--align-to {value!r} does not say which camera; this line has "
                f"{', '.join(cameras)} — use --align-to CAMERA=PATH")
    return targets


def resolve(line, camera, reference, target, model=DEFAULT_MODEL,
            cache_path=None, force=False):
    """(Alignment or None, probe image) for one camera against one new image."""
    probe_image = probe(target)
    solved = align_cache.resolve(line, [camera], {camera: reference},
                               {camera: probe_image}, model=model,
                               cache_path=cache_path, force=force,
                               report=print)
    alignment = solved.get(camera)
    if alignment is not None:
        mark = "ok " if alignment.accepted else "SKIP"
        print(f"  {mark} {camera:12s} d=({alignment.shift_px[0]:+7.2f},"
              f"{alignment.shift_px[1]:+6.2f})px rot={alignment.rotation_deg:+5.2f} "
              f"ncc {alignment.ncc_before:.3f}->{alignment.ncc_after:.3f}"
              + (f"  ({alignment.reason})" if alignment.reason else ""))
    return alignment, probe_image
