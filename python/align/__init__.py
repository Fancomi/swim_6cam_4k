"""Camera-drift correction for calibrated UVs.

A calibration binds mesh vertices to UVs in ONE image — the texture baked into
the FBX's .fbm when the model was made. The cameras here are not bolted down:
between the 202607 and 202608 underwater sessions each of the sixteen moved by
up to ~10px, and the femto water-entry camera moved ~47px. The UVs still point
where the pool used to be, so the stitch seams split and the water-entry
distances read wrong. Field data arrives with no calibration of its own, so the
only way back is to register the new image against the calibrated one and carry
that transform onto the UVs.

Four pure modules, cv2 + numpy only — no FBX SDK, so this runs on a machine that
can read an already-extracted mesh JSON but not the model:

    aligner   estimate one camera's drift as a normalised 3x3, or refuse
    mesh      apply it to a mesh's UVs
    probe     turn "the new data" (a clip, or an image) into one image to register
    cache     one (calibration, dataset) pair is solved once and reused

The estimator is phase-correlation-seeded pyramid ECC, NOT the SIFT+RANSAC of the
reference implementation this was modelled on. Pool tiling is periodic, and SIFT
false-locks on it: on underA1 and underA9 it reported -116px and -106px of
translation where the truth is ~0, because the NCC-versus-x-shift curve is a
plateau across +-200px with no unique peak. ECC on the raw image does not
converge on those two either; seeding it with a phase correlation at the top of a
3-level pyramid gets 16/16.

Known limitation, deliberately not fixed here: each camera is aligned on its own
while a seam belongs to two. The mean seam NCC rises 0.03~0.05, but 2~6 of the
15 underwater seams still get slightly worse, because the two cameras either side
were corrected in inconsistent directions. Fixing that means a joint optimisation
over the seams themselves — every camera's transform solved together against the
overlap residuals — which is the next step, not a tweak to this one.
"""
