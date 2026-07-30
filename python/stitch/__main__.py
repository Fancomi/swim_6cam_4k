"""Run one stitch line's steps: python -m python.stitch <profile> <steps>.

    python -m python.stitch overhead extract,still
    python -m python.stitch underwater still --real --blend-px 120
    python -m python.stitch overhead extract,asset,build,live --video-dir DIR

Steps are a table, the line is an argument. The alternative — a subcommand per
(line, step) pair — is what this replaces: five uw-* shell functions that
differed only in which paths they filled in, so a third line meant copying all
five again.

Skipping is per step kind, not uniform. extract/tex/asset are intermediates and
skip when their output is newer than their inputs (asset also compares a stamp of
its shaping options); --force redoes them. still/video always render: their
shaping is overridable from the command line and mtime cannot see that, so a
stale image that looks fresh would be a trap. build keeps its own check (the
executable exists).
"""
import argparse
from pathlib import Path

from python.stitch import export_ref_tex, profiles as P, render as R
from python.stitch import render_video as RV
from python.stitch import run as realtime
from python.stitch.profiles import StepError


def step_tex(profile, args):
    """Export one reference texture per camera (the frame the stitch sees)."""
    out_dir = profile.ref_tex_dir
    if (out_dir.is_dir() and any(out_dir.iterdir()) and not args.force):
        print(f"reference textures present: {out_dir}")
        return
    export_ref_tex.export(profile, out_dir=out_dir, video_dir=args.video_dir)


def step_still(profile, args):
    """Composite, grid diagnostic and fusion heatmap for one line.

    --real swaps the designer's calibration frames for the exported camera
    frames; the outputs take a _real suffix so the two never overwrite."""
    if not profile.mesh_json.is_file():
        raise StepError(
            f"mesh JSON missing (run the extract step): {profile.mesh_json}")
    if args.real:
        tex_dir = profile.ref_tex_dir
        if not tex_dir.is_dir():
            raise StepError(
                f"reference textures missing (run the tex step): {tex_dir}")
        tex_names = export_ref_tex.tex_names(profile)
        suffix = "_real"
    else:
        tex_dir = profile.still_tex_dir
        if not tex_dir.is_dir():
            raise StepError(
                f"still texture directory missing: {tex_dir} "
                "(set STITCH_GRID_DIR or ANNOTATION_PREVIEW_DATASET_ROOT)")
        tex_names = None
        suffix = ""
    out = profile.out_dir
    full_res = profile.full_res and not args.no_full_res
    # full_res rescales the still back to source-image height, which is a
    # different density than the .swasset bakes at (profiles.py: "ppm is the
    # .swasset canvas density ... full_res additionally rescales the *still*").
    # render_stills already derives the adaptive ppm from the source texture's
    # own height when ppm is None, so forcing profile.ppm here would size the
    # intermediate canvas for the wrong target and shift the auto bottom-crop
    # by a few rows. Only a profile without full_res (overhead) needs its ppm
    # substituted, since there full_res=False leaves render_stills no source
    # height to adapt to.
    default_ppm = None if full_res else profile.ppm
    R.render_stills(
        profile.mesh_json, tex_dir,
        out / f"stitch{suffix}.png", out / f"grid{suffix}.png",
        ppm=args.ppm if args.ppm is not None else default_ppm,
        neg_v=False,
        blend_px=args.blend_px if args.blend_px is not None else profile.blend_px,
        full_res=full_res,
        heatmap_path=out / f"heat{suffix}.png",
        tex_names=tex_names,
    )


def step_video(profile, args):
    """Stitch every camera's clip into one panorama mp4."""
    if not profile.mesh_json.is_file():
        raise StepError(
            f"mesh JSON missing (run the extract step): {profile.mesh_json}")
    full_res = profile.full_res and not args.no_full_res
    default_ppm = None if full_res else profile.ppm
    RV.render_video(
        profile.mesh_json, args.video_dir, profile.out_dir / "stitch.mp4",
        camera_ids=profile.camera_ids,
        clip_for=profile.clip_for,
        seconds=args.seconds_float,
        ppm=args.ppm if args.ppm is not None else default_ppm,
        neg_v=False,
        blend_px=args.blend_px if args.blend_px is not None else profile.blend_px,
        full_res=full_res,
        align=profile.sync == "manifest" and not args.no_align,
    )


# Offline first, then the realtime chain — the order a new line gets brought up.
STEPS = {
    "extract": realtime.step_extract,
    "tex": step_tex,
    "still": step_still,
    "video": step_video,
    "asset": realtime.step_asset,
    "build": realtime.step_build,
    "live": realtime.step_run,
}

# Steps that cannot work without clips to read.
_NEEDS_VIDEO = ("video", "live")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m python.stitch",
        description="Run a plane-stitch line's steps",
        epilog=f"steps: {', '.join(STEPS)}")
    parser.add_argument("profile", help=f"one of: {', '.join(P.names())}")
    parser.add_argument("steps", help="comma-separated steps, run in order")
    parser.add_argument("--video-dir", type=Path, default=None,
                        help="clip directory, one clip per camera")
    parser.add_argument("--real", action="store_true",
                        help="still: use the exported camera frames instead of "
                             "the designer's calibration textures")
    parser.add_argument("--ppm", type=float, default=None,
                        help="override the profile's pixels per metre")
    parser.add_argument("--blend-px", type=float, default=None,
                        help="override the profile's seam transition width")
    parser.add_argument("--no-full-res", action="store_true",
                        help="skip the rescale back to source height")
    parser.add_argument("--no-align", action="store_true",
                        help="read every clip from frame 0")
    parser.add_argument("--seconds", type=int, default=30,
                        help="live: run duration (default: %(default)s)")
    parser.add_argument("--seconds-float", type=float, default=None,
                        help="video: cap output duration; default is the whole "
                             "align window")
    parser.add_argument("--backend", default=realtime.default_backend(),
                        choices=("metal", "d3d11", "cudagl"))
    parser.add_argument("--fps", type=int, default=None,
                        help="live: override the render cadence")
    parser.add_argument("--no-preview", dest="preview", action="store_false",
                        default=True)
    parser.add_argument("--no-window", dest="window", action="store_false",
                        default=True, help="live: render offscreen")
    parser.add_argument("--encode", action="store_true",
                        help="live: also write HEVC")
    parser.add_argument("--encode-path", type=Path, default=None)
    parser.add_argument("--metrics", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None,
                        help="live: use this runtime config instead of "
                             "generating one")
    parser.add_argument("--no-clip-uv", dest="clip_uv", action="store_false",
                        default=True,
                        help="asset: keep pixels whose UV falls outside the "
                             "source image")
    parser.add_argument("--crop-bottom", default=None, metavar="auto|none|N",
                        help="asset: override the profile's bottom crop")
    parser.add_argument("--no-loop", dest="loop", action="store_false",
                        default=True,
                        help="live: stop when the clips run out instead of "
                             "restarting them")
    parser.add_argument("--force", action="store_true",
                        help="redo steps whose outputs look current")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    profile = P.get(args.profile)

    steps = [step.strip() for step in args.steps.split(",") if step.strip()]
    unknown = [step for step in steps if step not in STEPS]
    if unknown:
        raise SystemExit(f"unknown steps: {', '.join(unknown)}; "
                         f"valid: {', '.join(STEPS)}")
    if not steps:
        raise SystemExit(f"no steps given; valid: {', '.join(STEPS)}")

    needs_video = [step for step in steps if step in _NEEDS_VIDEO]
    if profile.ref_tex == "video" and "tex" in steps:
        needs_video.append("tex")
    if needs_video and args.video_dir is None and args.config is None:
        raise SystemExit(
            f"--video-dir is required for: {', '.join(sorted(set(needs_video)))}")

    # The realtime steps read the shaping values off `args`; fill the profile's
    # in so `live` behaves the same whether it was reached from here or from
    # python.stitch.run.
    args.asset_ppm = args.ppm if args.ppm is not None else profile.ppm
    if args.blend_px is None:
        args.blend_px = profile.blend_px
    if args.crop_bottom is None:
        args.crop_bottom = profile.crop_bottom
    if args.metrics is None:
        args.metrics = profile.metrics
    if args.encode_path is None:
        args.encode_path = (P.PROJECT_ROOT / "outputs" / "videos" /
                            f"{profile.name}_realtime.h265")
    args.align = not args.no_align

    profile.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        for step in steps:
            print(f"\n=== {profile.name}: {step} ===", flush=True)
            STEPS[step](profile, args)
    except StepError as error:
        raise SystemExit(f"error: {error}")
    print("\ndone.")


if __name__ == "__main__":
    main()
