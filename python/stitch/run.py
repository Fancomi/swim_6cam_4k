"""One-command underwater realtime stitch: extract, compile, build, run.

Cross-platform by construction — every step is the same Python here, and the
platform only decides which CMake generator, backend name, and executable path
to use. macOS gets Metal, Windows gets D3D11 (or CUDA/GL with --backend cudagl).

Each step is skipped when its output is already newer than its inputs, so the
common case (rerun after changing nothing) goes straight to the run.

    python -m python.stitch.run --video-dir DIR            # full pipeline
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

from python.stitch import render_video as RV

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUTS = PROJECT_ROOT / "outputs" / "underwater"
MODELS = PROJECT_ROOT / "inputs" / "underwater" / "models"
CONFIGS = PROJECT_ROOT / "inputs" / "configs"

MESH_JSON = OUTPUTS / "all_mesh.json"
ASSET = PROJECT_ROOT / "build" / "assets" / "generated" / "underwater_16.swasset"
# Records the asset-shaping options the current .swasset was built with, so
# changing any of them re-compiles even though the mesh JSON is untouched.
ASSET_STAMP = ASSET.with_suffix(".stamp")
# Left-to-right, matching the mesh order the extractor produces.
CAMERA_IDS = [f"underA{index}" for index in range(16, 0, -1)]
ASSET_PPM = 240.0
ASSET_BLEND_PX = 120.0
SOURCE_SIZE = (1280, 720)

STEPS = ("extract", "asset", "build", "run")


class StepError(RuntimeError):
    """A pipeline step failed; the message is already user-facing."""


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


def step_extract(args):
    fbx = Path(args.fbx)
    tex_dir = Path(args.tex_dir)
    if not fbx.is_file():
        raise StepError(f"FBX does not exist: {fbx}")
    if newer_than(MESH_JSON, fbx) and not args.force:
        print(f"mesh up to date: {MESH_JSON}")
        return
    run([python_bin(), "-m", "python.stitch.extract", fbx, MESH_JSON,
         "--tex-dir", tex_dir, "--planes-only"])


def asset_options(args):
    """The asset-shaping arguments, plus a stamp string identifying them."""
    options = ["--ppm", str(args.asset_ppm), "--no-neg-v",
               "--blend-px", str(args.blend_px),
               "--crop-bottom", str(args.crop_bottom),
               "--source-size", str(SOURCE_SIZE[0]), str(SOURCE_SIZE[1])]
    if args.clip_uv:
        options.append("--clip-uv")
    return options, " ".join(options)


def step_asset(args):
    if not MESH_JSON.is_file():
        raise StepError(f"mesh JSON missing (run the extract step): {MESH_JSON}")
    options, stamp = asset_options(args)
    current = ASSET_STAMP.read_text() if ASSET_STAMP.is_file() else None
    if (newer_than(ASSET, MESH_JSON) and current == stamp and not args.force):
        print(f"asset up to date: {ASSET}")
        return
    ASSET.parent.mkdir(parents=True, exist_ok=True)
    run([python_bin(), "-m", "python.assets.compile_runtime_asset",
         MESH_JSON, ASSET, "--camera-ids", *CAMERA_IDS, *options])
    ASSET_STAMP.write_text(stamp)


def step_build(args):
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


def lane_start_offsets(video_dir):
    """Per-camera milliseconds into each clip where the common time axis starts.

    Recorded clips do not share a t=0: each stream begins at its own decodable
    keyframe, placed inside the lookback window with GOP granularity, so the
    per-lane skew reaches seconds. The sample manifest carries the wall-clock
    truth; this reuses render_video's reading of it so the realtime path aligns
    by exactly the same formula the offline renderer and the player use.

    Returns {} when the sample has no usable manifest — live sources have none,
    and the runtime treats a missing offset as "read from the first frame"."""
    try:
        align_start, align_end, fps, cams = RV.load_manifest(video_dir)
    except SystemExit as error:
        print(f"  no wall-clock alignment: {error}")
        return {}
    order = [camera for camera in CAMERA_IDS if camera in cams]
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


def loop_period_ms(video_dir, offsets):
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


def write_config(path, video_dir, backend, encode_path, align=True,
                 loop=True):
    """Emit a runtime config naming the 16 lanes in left-to-right order.

    Written fresh each run so the clip directory and backend always match what
    was asked for; the C++ loader takes camera identity straight from these
    `source.<id>` lines."""
    clips = {}
    for camera in CAMERA_IDS:
        matches = sorted(Path(video_dir).glob(f"*_{camera}.ts"))
        if not matches:
            raise StepError(f"no clip for {camera} in {video_dir}")
        if len(matches) > 1:
            raise StepError(
                f"ambiguous clips for {camera}: {[m.name for m in matches]}")
        clips[camera] = matches[0]

    offsets = lane_start_offsets(video_dir) if align else {}
    period = loop_period_ms(video_dir, offsets) if loop else 0
    if loop:
        print(f"  looping every {period}ms" if period
              else "  looping at each file's own end (no manifest)")

    lines = [f"backend={backend}", "mode=realtime", "stage=full",
             f"asset={ASSET.as_posix()}"]
    for camera in CAMERA_IDS:
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
              f"metrics={(OUTPUTS / 'realtime.jsonl').as_posix()}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    aligned = sum(1 for camera in CAMERA_IDS if offsets.get(camera))
    print(f"wrote config {path} ({len(CAMERA_IDS)} lanes, "
          f"{aligned} with a start offset)")


def step_run(args):
    build_dir = build_dir_for(args.backend)
    executable = executable_for(build_dir)
    if not executable.is_file():
        raise StepError(f"executable missing (run the build step): {executable}")
    if not ASSET.is_file():
        raise StepError(f"asset missing (run the asset step): {ASSET}")

    encode_path = Path(args.encode_path)
    config = Path(args.config) if args.config else (
        CONFIGS / f"underwater_16_{args.backend}.conf")
    if args.config is None:
        write_config(config, args.video_dir, args.backend, encode_path,
                     align=args.align, loop=args.loop)
    elif not config.is_file():
        raise StepError(f"config does not exist: {config}")

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
        description="One-command underwater realtime stitch (macOS + Windows)")
    parser.add_argument("--video-dir", type=Path,
                        help="directory holding the 16 *_underAi.ts clips")
    parser.add_argument("--fbx", type=Path, default=MODELS / "all.fbx")
    parser.add_argument("--tex-dir", type=Path, default=MODELS / "all.fbm")
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
    parser.add_argument("--encode-path", type=Path,
                        default=PROJECT_ROOT / "outputs" / "videos" /
                        "underwater_realtime.h265")
    parser.add_argument("--metrics", type=Path,
                        default=OUTPUTS / "realtime.jsonl")
    parser.add_argument("--config", type=Path, default=None,
                        help="use this runtime config instead of generating one")
    parser.add_argument("--force", action="store_true",
                        help="redo steps even when their outputs look current")

    shaping = parser.add_argument_group(
        "composite shaping",
        "These control how the .swasset is baked, so the realtime stitch "
        "matches what python.stitch.render_video produces offline. "
        "Changing any of them recompiles the asset.")
    shaping.add_argument("--asset-ppm", type=float, default=ASSET_PPM,
                         help="output pixels per metre (default: %(default)s)")
    shaping.add_argument("--blend-px", type=float, default=ASSET_BLEND_PX,
                         help="vertical seam transition width in pixels; "
                              "0 is a hard cut (default: %(default)s)")
    shaping.add_argument("--no-clip-uv", dest="clip_uv", action="store_false",
                         default=True,
                         help="keep pixels whose UV falls outside the source "
                              "image (the GPU mirror-samples them); clipping "
                              "is on by default to match the offline renderer")
    shaping.add_argument("--crop-bottom", default="auto",
                         metavar="auto|none|N",
                         help="drop bottom rows the shorter planes leave "
                              "uncovered (default: %(default)s)")
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
            handlers[step](args)
    except StepError as error:
        raise SystemExit(f"error: {error}")
    print("\ndone.")


if __name__ == "__main__":
    main()
