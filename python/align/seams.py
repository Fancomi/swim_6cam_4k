"""Score a stitch line's registration: how well do neighbours agree on the seam?

An alignment's own NCC gain says the transform fits the new image; it does not
say the panorama got better. The question that matters for a stitch line is
whether two cameras, remapped onto the shared canvas, paint the SAME thing where
they overlap. That is measurable without any ground truth: take each adjacent
pair's overlap region and correlate the two remapped images inside it.

Deliberately NOT a best-shift search. An earlier version reported the offset that
would maximise correlation, which sounds more informative but is unusable here:
the underwater overlaps are 0.5m of near-uniform tiled floor, so the search
saturates at its own bound (24px of a 25px radius) on most pairs both before and
after alignment, and the number stops discriminating. Correlation at zero shift
answers the question actually being asked — are they registered — and moved
0.627 -> 0.676 on the same data where the shift search could not tell the two
apart.

Lives in `python/align/` rather than in `python/stitch/`: it is how a correction
is judged, it is needed identically by the align entry point and by any future
joint optimisation, and it imports the stitch chain's compose module the same way
any consumer of that geometry would.
"""
import cv2
import numpy as np

from python.align.aligner import ncc
from python.stitch import compose as C

# A pair overlapping less than this is not a seam worth scoring — it is two
# blocks that barely touch, where the correlation is dominated by whichever few
# hundred pixels happen to be in the sliver.
MIN_OVERLAP_PX = 2000


def canvas_and_layers(meshes, profile, textures, ppm=None):
    """The canvas and per-lane remap layers, exactly as the still renderer builds
    them.

    Reusing `compose` rather than re-deriving the projection matters: a metric
    computed through a second, subtly different projection would report its own
    rounding as a registration change."""
    ppm = profile.ppm if ppm is None else ppm
    canvas = C.Canvas(meshes, ppm, margin=profile.still_margin)
    layers = [C.build_remap(mesh, canvas,
                            (texture.shape[1], texture.shape[0]),
                            clip=profile.clip_uv)
              for mesh, texture in zip(meshes, textures)]
    return canvas, layers


def seam_scores(meshes, profile, textures, ppm=None):
    """Per adjacent pair, the NCC of the two remaps inside their overlap.

    None for a pair that does not overlap enough to judge. Lanes are adjacent in
    profile order, which for the plane lines is world-X order, so pair i is
    camera i against camera i+1.
    """
    _canvas, layers = canvas_and_layers(meshes, profile, textures, ppm)
    warped = [cv2.cvtColor(cv2.remap(texture, layer[0], layer[1],
                                     cv2.INTER_LINEAR),
                           cv2.COLOR_BGR2GRAY).astype(np.float32)
              for layer, texture in zip(layers, textures)]
    scores = []
    for index in range(len(layers) - 1):
        overlap = (layers[index][2] > 0) & (layers[index + 1][2] > 0)
        if overlap.sum() < MIN_OVERLAP_PX:
            scores.append(None)
            continue
        scores.append(ncc(warped[index], warped[index + 1], overlap))
    return scores


def summarise(scores, baseline=None):
    """Mean / min / count, plus how many pairs a baseline beat.

    `worse` is reported rather than smoothed over because it is the honest
    limitation of per-camera alignment: two cameras either side of a seam are
    corrected independently, so a seam can end up worse even as the mean rises.
    A summary that only showed the mean would hide that."""
    values = [score for score in scores if score is not None]
    result = {
        "seams": len(values),
        "mean": float(np.mean(values)) if values else 0.0,
        "min": float(min(values)) if values else 0.0,
    }
    if baseline is not None:
        pairs = [(now, was) for now, was in zip(scores, baseline)
                 if now is not None and was is not None]
        # 0.005 of correlation is below what re-probing the same clip reproduces,
        # so counting anything smaller as a regression would count noise.
        result["worse"] = sum(1 for now, was in pairs if now < was - 0.005)
        result["better"] = sum(1 for now, was in pairs if now > was + 0.005)
        base = [was for _now, was in pairs]
        result["delta"] = (float(np.mean([now for now, _ in pairs])
                                 - np.mean(base)) if pairs else 0.0)
    return result


def format_summary(summary):
    """One line of text for a console report."""
    text = (f"mean={summary['mean']:.4f} min={summary['min']:.3f} "
            f"({summary['seams']} seams)")
    if "delta" in summary:
        text += (f" delta={summary['delta']:+.4f} "
                 f"better={summary['better']} worse={summary['worse']}")
    return text
