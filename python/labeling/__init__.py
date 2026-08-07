"""Browser labelers and the dataset snapshot index they work over.

    server.py          serve one labeler over http and open it
    snapshots.py       where a camera's snapshot frames live
    merge_overhead.py  collapse a camera's snapshots into one UV reference
    frames.py          unified entry: organize / auto_merge / merge / grid / label
    mask_labeler/      keep-region strokes
    dot_labeler/       point annotation
"""
