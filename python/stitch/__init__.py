"""Camera-line stitching: FBX in, panorama out, offline and realtime.

One module set drives every line — the six-camera pool, the 16-plane underwater
panorama, the 2-plane overhead lane. The per-line differences (model, camera ids,
pixel density, blend, time-alignment policy) are data in profiles.py, so adding a
line touches one record and nothing else.

    profiles.py    what differs between lines
    extract.py     FBX -> mesh JSON
    compose.py     the stitch: canvas, remap, weights, composite
    render.py      still + grid diagnostic + fusion heatmap
    render_video.py  one clip per camera -> panorama mp4
    asset.py       mesh JSON -> GPU .swasset
    run.py         build the executable, write a config, run it live
    __main__.py    the step table: python -m python.stitch LINE STEPS
"""
