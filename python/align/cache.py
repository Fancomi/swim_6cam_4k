"""Solve a line's cameras once per (calibration, dataset) pair, then reuse it.

Solving all sixteen underwater cameras takes ~10s — 0.35s of median probing plus
0.25~0.40s of ECC each. That is cheap enough to do without asking, and far too
expensive to repeat for every still, every video render and every algorithm run
over the same day's data. So the result is cached, and the cache key is the thing
that actually determines the answer: WHICH calibration texture and WHICH probe
image went in.

Fingerprints are content hashes of those two images, not paths or mtimes. The
underwater textures have been re-baked in place across FBX revisions (8.14-02
swapped the mask composites for bare backgrounds without renaming anything), so a
path is not an identity and an mtime only catches the times someone touched the
file. Hashing the decoded pixels means a re-baked texture invalidates its entry
and an untouched one does not, whatever the filesystem says.
"""
import hashlib
import json
from pathlib import Path

import numpy as np

from python.align.aligner import DEFAULT_MODEL, Alignment, solve

# Bumped when a stored field changes meaning. An older file is then discarded
# rather than misread — a silently misinterpreted matrix is worse than a re-solve.
FORMAT = "align/v1"


def fingerprint(image):
    """A content hash of a decoded image.

    Over the pixels rather than the file bytes so a re-encode of identical
    pixels (PNG vs JPEG of the same frame, which this dataset has plenty of)
    does not force a re-solve."""
    array = np.ascontiguousarray(image)
    digest = hashlib.sha256()
    digest.update(str(array.shape).encode())
    digest.update(array.tobytes())
    return digest.hexdigest()[:16]


def _entry_key(reference, probe_image, model):
    return f"{fingerprint(reference)}:{fingerprint(probe_image)}:{model}"


def load(path):
    """The stored document, or None when it is absent or of another format."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if document.get("format") != FORMAT:
        return None
    return document


def save(path, line, model, entries):
    """Write the cache document. `entries` is {camera: {...}} as built below."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": FORMAT, "line": line,
                                "model": model, "cameras": entries},
                               indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return path


def resolve(line, cameras, references, probes, model=DEFAULT_MODEL,
            cache_path=None, force=False, report=None):
    """{camera: Alignment or None}, solving only what the cache cannot answer.

    `references` and `probes` are {camera: BGR image}; a camera missing from
    either gets None, meaning "use the calibration as it is". The returned
    alignments include the rejected ones — a caller reporting on the run wants to
    say why a camera was left alone, and `Alignment.accepted` carries that.

    `report` is a callable given one summary line; None stays silent, which is
    what a library call (and a test) wants. Printing unconditionally from here
    made the unit tests emit nine lines of progress about nothing.
    """
    document = None if force else load(cache_path) if cache_path else None
    stored = (document or {}).get("cameras", {})
    entries, results, reused = {}, {}, 0
    for camera in cameras:
        reference = references.get(camera)
        probe_image = probes.get(camera)
        if reference is None or probe_image is None:
            results[camera] = None
            continue
        key = _entry_key(reference, probe_image, model)
        cached = stored.get(camera)
        if cached is not None and cached.get("key") == key:
            alignment = Alignment.from_dict(cached["alignment"])
            reused += 1
        else:
            alignment = solve(reference, probe_image, model)
        results[camera] = alignment
        entries[camera] = {"key": key, "alignment": alignment.as_dict()}
    if cache_path is not None and entries:
        save(cache_path, line, model, entries)
    if report is not None:
        accepted = sum(1 for a in results.values()
                       if a is not None and a.accepted)
        report(f"  alignments: {len(entries) - reused} solved, {reused} reused "
               f"from cache, {accepted}/{len(entries)} accepted")
    return results


def report_rows(cameras, alignments):
    """One CSV-ready dict per camera, for the run summary."""
    rows = []
    for camera in cameras:
        alignment = alignments.get(camera)
        if alignment is None:
            rows.append({"camera": camera, "accepted": "", "dx_px": "",
                         "dy_px": "", "rotation_deg": "", "gain": "",
                         "reason": "no probe"})
            continue
        rows.append({
            "camera": camera,
            "accepted": "1" if alignment.accepted else "0",
            "dx_px": f"{alignment.shift_px[0]:.2f}",
            "dy_px": f"{alignment.shift_px[1]:.2f}",
            "rotation_deg": f"{alignment.rotation_deg:.3f}",
            "gain": f"{alignment.gain:+.4f}",
            "reason": alignment.reason,
        })
    return rows


REPORT_COLUMNS = ["camera", "accepted", "dx_px", "dy_px", "rotation_deg",
                  "gain", "reason"]
