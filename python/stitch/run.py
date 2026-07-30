"""One-command plane stitch: extract, compile, build, run.

Cross-platform by construction — every step is the same Python here, and the
platform only decides which CMake generator, backend name, and executable path
to use. macOS gets Metal, Windows gets D3D11 (or CUDA/GL with --backend cudagl).

Which model, how many lanes, what pixel density, whether the clips carry a wall
clock: all of that is the profile's, not this module's. Adding a stitch line
does not touch this file.

Each step is skipped when its output is already newer than its inputs, so the
common case (rerun after changing nothing) goes straight to the run.

    python -m python.stitch.run --profile overhead --video-dir DIR
    python -m python.stitch.run --video-dir DIR --seconds 30 --encode
    python -m python.stitch.run --steps asset,run          # skip extraction
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from python.stitch import profiles as P
from python.stitch import render_video as RV
from python.stitch.profiles import CONFIGS, PROJECT_ROOT, StepError

STEPS = ("extract", "asset", "build", "run")


def is_windows():
    return platform.system() == "Windows"


def default_backend():
    return "d3d11" if is_windows() else "metal"


def build_dir_for(backend):
    return PROJECT_ROOT / "build" / ("win-" + backend if is_windows()
                                     else backend + "-release")


def executable_for(build_dir):
    if is_windows():
        # Multi-config generators put binaries under a per-config subdirectory.
        for candidate in (build_dir / "Release" / "swim_realtime.exe",
                          build_dir / "swim_realtime.exe"):
            if candidate.is_file():
                return candidate
        return build_dir / "Release" / "swim_realtime.exe"
    return build_dir / "swim_realtime"


def python_bin():
    """The interpreter to hand CMake; prefer the project venv over ours."""
    venv = (PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if is_windows()
                                      else "bin/python"))
    return venv if venv.is_file() else Path(sys.executable)


def run(command, *, cwd=PROJECT_ROOT, env=None):
    printable = " ".join(str(part) for part in command)
    print(f"$ {printable}", flush=True)
    result = subprocess.run([str(part) for part in command], cwd=str(cwd),
                            env=env, check=False)
    if result.returncode != 0:
        raise StepError(f"command failed ({result.returncode}): {printable}")


def newer_than(target, *sources):
    """True when `target` exists and is at least as new as every source."""
    target = Path(target)
    if not target.is_file():
        return False
    target_mtime = target.stat().st_mtime
    return all(not Path(s).is_file() or Path(s).stat().st_mtime <= target_mtime
               for s in sources)


def stamp_path(profile):
    """Where the asset's shaping fingerprint lives, one file per profile."""
    return profile.asset.with_suffix(".stamp")


def step_extract(profile, args):
    if not profile.fbx.is_file():
        raise StepError(f"FBX does not exist: {profile.fbx}")
    if newer_than(profile.mesh_json, profile.fbx) and not args.force:
        print(f"mesh up to date: {profile.mesh_json}")
        return
    command = [python_bin(), "-m", "python.stitch.extract",
               profile.fbx, profile.mesh_json, "--tex-dir", profile.tex_dir]
    if profile.planes_only:
        command.append("--planes-only")
    run(command)


def asset_options(profile, args):
    """The asset-shaping arguments, plus a stamp string identifying them.

    The stamp leads with the profile name so two lines writing sibling .stamp
    files can never satisfy each other's up-to-date check."""
    options = ["--ppm", str(args.asset_ppm), "--no-neg-v",
               "--blend-px", str(args.blend_px),
               "--crop-bottom", str(args.crop_bottom),
               "--source-size", str(profile.source_size[0]),
               str(profile.source_size[1])]
    if args.clip_uv:
        options.append("--clip-uv")
    return options, " ".join([profile.name, *options])


def step_asset(profile, args):
    if not profile.mesh_json.is_file():
        raise StepError(
            f"mesh JSON missing (run the extract step): {profile.mesh_json}")
    options, stamp = asset_options(profile, args)
    stamp_file = stamp_path(profile)
    current = stamp_file.read_text() if stamp_file.is_file() else None
    if (newer_than(profile.asset, profile.mesh_json) and current == stamp
            and not args.force):
        print(f"asset up to date: {profile.asset}")
        return
    profile.asset.parent.mkdir(parents=True, exist_ok=True)
    run([python_bin(), "-m", "python.assets.compile_runtime_asset",
         profile.mesh_json, profile.asset,
         "--camera-ids", *profile.camera_ids, *options])
    stamp_file.write_text(stamp)


def step_build(profile, args):        # noqa: ARG001 - signature parity
    build_dir = build_dir_for(args.backend)
    executable = executable_for(build_dir)
    if executable.is_file() and not args.force:
        print(f"executable present: {executable}")
        return
    if shutil.which("cmake") is None:
        raise StepError("cmake is not on PATH")
    configure = ["cmake", "-S", PROJECT_ROOT, "-B", build_dir,
                 f"-DPython3_EXECUTABLE={python_bin()}"]
    if is_windows():
        configure += ["-G", "Visual Studio 17 2022", "-A", "x64"]
    else:
        if shutil.which("ninja") is not None:
            configure += ["-G", "Ninja"]
        configure += ["-DCMAKE_BUILD_TYPE=Release"]
    run(configure)
    build = ["cmake", "--build", build_dir, "--target", "swim_realtime"]
    if is_windows():
        build += ["--config", "Release"]
    else:
        build += ["-j", str(os.cpu_count() or 4)]
    run(build)


def lane_start_offsets(profile, video_dir):
    """Per-camera milliseconds into each clip where the common time axis starts.

    Recorded clips do not always share a t=0: each stream begins at its own
    decodable keyframe, placed inside the lookback window with GOP granularity,
    so the per-lane skew reaches seconds. Profiles whose samples carry that
    wall-clock truth (sync="manifest") reuse render_video's reading of it, so the
    realtime path aligns by exactly the same formula as the offline renderer.

    Profiles with sync="none" return {} without looking: their recordings have
    no manifest by design, and reporting that as an exception every run would be
    noise. A manifest-bearing profile whose manifest is missing degrades to the
    same empty result, but says so."""
    if profile.sync != "manifest":
        return {}
    try:
        align_start, align_end, fps, cams = RV.load_manifest(video_dir)
    except SystemExit as error:
        print(f"  no wall-clock alignment: {error}")
        return {}
    order = [camera for camera in profile.camera_ids if camera in cams]
    starts, report = RV.alignment_plan(align_start, align_end, fps, cams, order)
    offsets = {}
    for camera, entry in zip(order, report):
        # A negative skew means the clip begins after align_start; that lane has
        # no coverage at t=0 and its offset clamps to zero.
        offsets[camera] = max(0, entry["skew_ms"])
    skews = [entry["skew_ms"] for entry in report]
    print(f"  wall-clock align window {(align_end - align_start) / 1000:.3f}s; "
          f"lane skew {min(skews)}..{max(skews)}ms")
    for entry in report:
        if entry["late_start"]:
            print(f"  QC {entry['cam']}: starts {-entry['skew_ms']}ms after "
                  "align_start (no coverage at t=0)")
    return offsets


def loop_period_ms(profile, video_dir, offsets):
    """Shortest usable span across lanes, in ms — the common content period.

    Each lane can play from its aligned start to its own last decodable frame.
    Those spans differ by tens of milliseconds, so restarting each lane at its
    own end would let them drift apart on every pass. Wrapping every lane on the
    shortest span keeps them locked together indefinitely.

    Returns 0 when there is no manifest, which tells the runtime to use each
    file's natural end."""
    try:
        _align_start, _align_end, _fps, cams = RV.load_manifest(video_dir)
    except SystemExit:
        return 0
    spans = []
    for camera, info in cams.items():
        last = info.get("last_decodable_ms")
        anchor = info.get("keyframe_ms")
        if last is None or anchor is None:
            continue
        spans.append(last - anchor - offsets.get(camera, 0))
    if not spans:
        return 0
    return max(0, min(spans))


def write_config(profile, path, video_dir, backend, encode_path, align=True,
                 loop=True):
    """Emit a runtime config naming the profile's lanes left-to-right.

    Written fresh each run so the clip directory and backend always match what
    was asked for; the C++ loader takes camera identity straight from these
    `source.<id>` lines."""
    clips = {camera: profile.clip_for(video_dir, camera)
             for camera in profile.camera_ids}
    offsets = lane_start_offsets(profile, video_dir) if align else {}
    period = loop_period_ms(profile, video_dir, offsets) if loop else 0
    if loop:
        print(f"  looping every {period}ms" if period
              else "  looping at each file's own end (no manifest)")

    lines = [f"backend={backend}", "mode=realtime", "stage=full",
             f"asset={profile.asset.as_posix()}"]
    for camera in profile.camera_ids:
        lines.append(f"source.{camera}={clips[camera].as_posix()}")
        if offsets.get(camera):
            lines.append(f"source.{camera}.start_ms={offsets[camera]}")
    lines += [f"loop_sources={'true' if loop else 'false'}",
              f"stop_at_eof={'false' if loop else 'true'}",
              f"loop_period_ms={period}",
              "fps_num=30000", "fps_den=1001",
              "preview=true", "encode=false", "diagnostic_replacement=false",
              f"encode_path={Path(encode_path).as_posix()}",
              "stale_ms=100", "replace_ms=1000",
              "decode_surface_pool=8", "decode_ticket_pool=16",
              "render_inflight=3", "output_pool=4",
              "duration_seconds=10",
              f"metrics={profile.metrics.as_posix()}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    aligned = sum(1 for camera in profile.camera_ids if offsets.get(camera))
    print(f"wrote config {path} ({len(profile.camera_ids)} lanes, "
          f"{aligned} with a start offset)")


def step_run(profile, args):
    build_dir = build_dir_for(args.backend)
    executable = executable_for(build_dir)
    if not executable.is_file():
        raise StepError(f"executable missing (run the build step): {executable}")
    if not profile.asset.is_file():
        raise StepError(f"asset missing (run the asset step): {profile.asset}")

    encode_path = Path(args.encode_path)
    config = Path(args.config) if args.config else profile.config_path(args.backend)
    if args.config is None:
        write_config(profile, config, args.video_dir, args.backend, encode_path,
                     align=args.align, loop=args.loop)
    elif not config.is_file():
        raise StepError(f"config does not exist: {config}")

    # Loop controls are not repeated on the command line: write_config already
    # emitted loop_sources / stop_at_eof / loop_period_ms, and a --loop here
    # would silently override whatever a caller-supplied --config asked for.
    command = [executable, "--config", config,
               f"--duration-seconds={args.seconds}",
               f"--preview={'true' if args.preview else 'false'}",
               f"--preview-visible={'true' if args.window else 'false'}",
               f"--metrics={args.metrics}"]
    if args.encode:
        encode_path.parent.mkdir(parents=True, exist_ok=True)
        command += ["--encode=true", "--encode-sink=file",
                    f"--encode-path={encode_path}"]
    if args.fps is not None:
        command += [f"--fps={args.fps}"]
    Path(args.metrics).parent.mkdir(parents=True, exist_ok=True)
    run(command)
    print(f"metrics -> {args.metrics}")
    if args.encode:
        print(f"HEVC    -> {encode_path}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="One-command plane stitch (macOS + Windows)")
    parser.add_argument("--profile", default="underwater",
                        choices=P.names(),
                        help="stitch line to run (default: %(default)s)")
    parser.add_argument("--video-dir", type=Path,
                        help="directory holding one clip per camera")
    parser.add_argument("--backend", default=default_backend(),
                        choices=("metal", "d3d11", "cudagl"))
    parser.add_argument("--steps", default=",".join(STEPS),
                        help=f"comma-separated subset of {','.join(STEPS)}")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--fps", type=int, default=None,
                        help="override the render cadence (default: clip fps)")
    parser.add_argument("--no-preview", dest="preview", action="store_false",
                        default=True)
    parser.add_argument("--no-window", dest="window", action="store_false",
                        default=True, help="render offscreen (no preview window)")
    parser.add_argument("--encode", action="store_true",
                        help="also write HEVC to --encode-path")
    parser.add_argument("--encode-path", type=Path, default=None,
                        help="HEVC destination (default: "
                             "outputs/videos/<profile>_realtime.h265)")
    parser.add_argument("--metrics", type=Path, default=None,
                        help="metrics JSONL (default: the profile's)")
    parser.add_argument("--config", type=Path, default=None,
                        help="use this runtime config instead of generating one")
    parser.add_argument("--force", action="store_true",
                        help="redo steps even when their outputs look current")

    shaping = parser.add_argument_group(
        "composite shaping",
        "These control how the .swasset is baked, so the realtime stitch "
        "matches what python.stitch.render_video produces offline. Each "
        "defaults to the profile's value; changing any recompiles the asset.")
    shaping.add_argument("--asset-ppm", type=float, default=None,
                         help="output pixels per metre")
    shaping.add_argument("--blend-px", type=float, default=None,
                         help="vertical seam transition width in pixels; "
                              "0 is a hard cut")
    shaping.add_argument("--no-clip-uv", dest="clip_uv", action="store_false",
                         default=True,
                         help="keep pixels whose UV falls outside the source "
                              "image (the GPU mirror-samples them); clipping "
                              "is on by default to match the offline renderer")
    shaping.add_argument("--crop-bottom", default=None,
                         metavar="auto|none|N",
                         help="drop bottom rows the shorter planes leave "
                              "uncovered")
    shaping.add_argument("--no-loop", dest="loop", action="store_false",
                         default=True,
                         help="stop when the clips run out instead of "
                              "restarting them")
    shaping.add_argument("--no-align", dest="align", action="store_false",
                         default=True,
                         help="ignore the manifest wall clocks and read every "
                              "clip from its first frame")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    profile = P.get(args.profile)
    # Unset shaping options fall back to the profile, so `--profile overhead`
    # alone bakes exactly what the design specifies.
    if args.asset_ppm is None:
        args.asset_ppm = profile.ppm
    if args.blend_px is None:
        args.blend_px = profile.blend_px
    if args.crop_bottom is None:
        args.crop_bottom = profile.crop_bottom
    if args.metrics is None:
        args.metrics = profile.metrics
    if args.encode_path is None:
        args.encode_path = (PROJECT_ROOT / "outputs" / "videos" /
                            f"{profile.name}_realtime.h265")

    steps = [step.strip() for step in args.steps.split(",") if step.strip()]
    unknown = [step for step in steps if step not in STEPS]
    if unknown:
        raise SystemExit(f"unknown steps: {', '.join(unknown)}; "
                         f"valid: {', '.join(STEPS)}")
    if "run" in steps and args.config is None and args.video_dir is None:
        raise SystemExit("--video-dir is required (or pass --config)")

    handlers = {"extract": step_extract, "asset": step_asset,
                "build": step_build, "run": step_run}
    try:
        for step in steps:
            print(f"\n=== {step} ===", flush=True)
            handlers[step](profile, args)
    except StepError as error:
        raise SystemExit(f"error: {error}")
    print("\ndone.")


if __name__ == "__main__":
    main()
