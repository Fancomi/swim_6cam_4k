"""Plane-stitch tasks: FBX extraction and N-plane horizontal composite.

One module set drives every stitch line; the per-line differences (model,
camera ids, pixel density, seam width, time-alignment policy) live as data in
profiles.py. Geometry and blending are reused from the pool pipeline
(python.assets.extract_fbx, python.validation.reference_renderer) rather than
copied.
"""
