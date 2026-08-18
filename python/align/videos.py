"""Render a calibration cell's off/on panorama videos at full length.

The align matrix's `--video` renders a short clip by default (~4s) because a
long one costs ~90s per version. Sometimes you want the whole thing: a swimmer
crossing a mis-registered seam jumps, stretches or briefly duplicates, and the
moment worth watching may not fall inside a short window.

By default this renders EVERY sample in the era's clip directory (202607 has 12
samples), not a single one. A sample that has no align.json yet is SOLVED on the
spot (probe its own median background, ECC each camera, cache the result under
the same align.json the matrix would have written) — nothing is skipped. Both
versions of each sample share the same clips and time alignment, so they are
frame-for-frame comparable, and the two versions differ only in the UVs.

    ./scripts/run_align_videos.sh                       # every 202607 sample, underwater2
    ./scripts/run_align_videos.sh --line underwater --data 202608
    ./scripts/run_align_videos.sh --sample swb_..._12  # one sample
    ./scripts/run_align_videos.sh --seconds 6          # cap each window
    ./scripts/run_align_videos.sh --force              # re-solve + re-render all
"""
import argparse
from pathlib import Path

from python.align.aligner import Alignment
from python.common.paths import OUTPUTS, dataset_root
from python.stitch import profiles as P
from python.stitch import render_video as RV

UNDER_VIDEOS = "SWIM_UNDER_VIDEOS_ROOT"
UNDER_VIDEOS_DEFAULT = ("/Users/penghaotian/Downloads/DATAS/SWIMMING/"
                        "swimming-xlj-under-videos")

LINES = ("underwater", "underwater2")
DATA_DAYS = ("202607", "202608")


def _samples(day, sample=None):
    """The clip directories of one data era.

    `sample` pins a single one; otherwise the whole day is enumerated (each entry
    is a directory holding `*_underA*.ts` — the underwater recorder's clips, which
    is what these two lines play). A directory that exists but holds no clips
    (the recorder's leftovers) is skipped rather than failing the batch."""
    root = dataset_root(UNDER_VIDEOS, UNDER_VIDEOS_DEFAULT) / day
    if not root.is_dir():
        raise SystemExit(f"no data directory for {day}: {root}")
    if sample is not None:
        if not (root / sample).is_dir():
            raise SystemExit(f"sample directory missing: {root / sample}")
        return [sample]
    return sorted(entry.name for entry in root.iterdir()
                  if entry.is_dir() and any(entry.glob("*_underA*.ts")))


def _alignments(line, day, sample, video_dir, force=False):
    """This sample's drift correction, solving it if not cached.

    The cache key is (reference texture, probe image, model), and the probe is
    each sample's own median background — so a sample that was never in the
    matrix has no cache entry and is SOLVED HERE rather than skipped. Solving one
    line against one sample is ~10s (median probe + ECC per camera); the result
    is cached under the same align.json the matrix would have written, so a
    re-run renders straight from it.
    """
    from python.stitch import align as SA
    alignments, _probes, _key = SA.resolve(
        profile_for(line), video_dir, key=f"{day}_{sample}",
        use_cache=not force)
    return alignments


def render_cell(line, day, sample, seconds=None, out_suffix="full_video",
                force=False):
    """off/on full-length panoramas for one (line, day, sample) cell.

    The alignment is solved on demand when the cache is missing (see
    `_alignments`); existing video files are skipped, not re-rendered — rendering
    is the expensive part and the cache already pins the inputs.
    """
    profile = profile_for(line)
    key = f"{day}_{sample}"
    video_dir = (dataset_root(UNDER_VIDEOS, UNDER_VIDEOS_DEFAULT)
                 / day / sample)
    if not video_dir.is_dir():
        raise SystemExit(f"clip directory does not exist: {video_dir}")
    alignments = _alignments(line, day, sample, video_dir, force=force)

    written = []
    for tag, applied in (("off", None), ("on", alignments)):
        out = OUTPUTS / line / "align" / key / f"{out_suffix}_{tag}.mp4"
        if out.is_file() and not force:
            print(f"  {tag}: {out.name} exists, skip")
            written.append(out)
            continue
        print(f"  rendering {line} x {day} -> {out.name}", flush=True)
        width, height, frames = RV.render(
            profile, video_dir, out, seconds=seconds, full_res=False,
            alignments=applied)
        print(f"  {tag:3s}: {out.name} {width}x{height} {frames}f")
        written.append(out)
    return written


def profile_for(line):
    """The stitch profile for a calibration line."""
    return P.get(line)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m python.align.videos",
        description="Render a cell's off/on panoramas at full length, for every "
                    "sample of one data era by default",
        epilog="lines: " + ", ".join(LINES) + "; days: " + ", ".join(DATA_DAYS))
    parser.add_argument("--line", choices=LINES, default="underwater2",
                        help="calibration line (default: %(default)s)")
    parser.add_argument("--data", choices=DATA_DAYS, default="202607",
                        help="data era (default: %(default)s)")
    parser.add_argument("--sample", default=None,
                        help="one clip directory; default renders the whole era")
    parser.add_argument("--seconds", type=float, default=None,
                        help="cap each output; default is the full manifest "
                             "window (~12s)")
    parser.add_argument("--out-suffix", default="full_video",
                        help="output basename (default: %(default)s)")
    parser.add_argument("--force", action="store_true",
                        help="re-solve the alignment and re-render even when "
                             "cached")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    samples = _samples(args.data, args.sample)
    print(f"=== {args.line} x {args.data}: {len(samples)} sample(s) ===", flush=True)
    for sample in samples:
        render_cell(args.line, args.data, sample, seconds=args.seconds,
                    out_suffix=args.out_suffix, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
