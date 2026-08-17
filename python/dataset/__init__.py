"""Custody of the calibration inputs: what should be in inputs/, and is it.

The data is not in git (see docs/DATA.md), so "do I have the right files" stops
being something git answers. This package answers it instead, and it is a package
of its own because the question spans both line registries — the six stitch lines
and the two overlay lines — while belonging to neither.
"""
