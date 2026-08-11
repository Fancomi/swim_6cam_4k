"""The realtime half: build the executable, write a config, run it.

Cross-platform by construction — every decision is made in Python here, and the
platform only picks a CMake generator, a backend name and an executable path.
macOS gets Metal, Windows gets D3D11 (or CUDA/GL with --backend cudagl).

Nothing here knows which line it is running.
"""
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from python.stitch import render_video as RV
from python.stitch.profiles import PROJECT_ROOT, StepError


def is_windows():
    return platform.system() == "Windows"


def default_backend():
    return "d3d11" if is_windows() else "metal"


def build_dir_for(backend):
    return PROJECT_ROOT / "build" / ("win-" + backend if is_windows()
                                    else backend + "-release")


def executable_for(build_dir):
    if not is_windows():
        return build_dir / "swim_realtime"
    # Multi-config generators put binaries under a per-config subdirectory.
    for candidate in (build_dir / "Release" / "swim_realtime.exe",
                      build_dir / "swim_realtime.exe"):
        if candidate.is_file():
            return candidate
    return build_dir / "Release" / "swim_realtime.exe"


def python_bin():
    """The interpreter to hand CMake; prefer the project venv over ours."""
    venv = PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if is_windows()
                                     else "bin/python")
    return venv if venv.is_file() else Path(sys.executable)


def run(command, *, cwd=PROJECT_ROOT):
    printable = " ".join(str(part) for part in command)
    print(f"$ {printable}", flush=True)
    result = subprocess.run([str(part) for part in command], cwd=str(cwd),
                            check=False)
    if result.returncode != 0:
        raise StepError(f"command failed ({result.returncode}): {printable}")


def newer_than(target, *sources):
    """True when `target` exists and is at least as new as every source."""
    target = Path(target)
    if not target.is_file():
        return False
    stamp = target.stat().st_mtime
    return all(not Path(s).is_file() or Path(s).stat().st_mtime <= stamp
               for s in sources)


def stamp_path(profile):
    """Where the asset's shaping fingerprint lives, one file per line."""
    return profile.asset.with_suffix(".stamp")


def shaping_stamp(profile, args):
    """A string identifying every option the baked asset depends on.

    Leads with the line name so lines writing sibling .stamp files can never
    satisfy each other's up-to-date check. mtime alone cannot see a changed
    --ppm, so without this a reshaped asset would be skipped as current."""
    return " ".join(str(part) for part in (
        profile.name, args.ppm if args.ppm is not None else profile.ppm,
        args.blend_px if args.blend_px is not None else profile.blend_px,
        args.crop_bottom if args.crop_bottom is not None else profile.crop_bottom,
        profile.clip_uv, profile.neg_v, profile.neg_u, profile.source_size))


def step_extract(profile, args):
    from python.stitch import extract
    if newer_than(profile.mesh_json, profile.fbx) and not args.force:
        print(f"mesh up to date: {profile.mesh_json}")
        return
    extract.extract(profile)


def step_asset(profile, args):
    from python.stitch import asset
    stamp = shaping_stamp(profile, args)
    stamp_file = stamp_path(profile)
    current = stamp_file.read_text() if stamp_file.is_file() else None
    if (newer_than(profile.asset, profile.mesh_json) and current == stamp
            and not args.force):
        print(f"asset up to date: {profile.asset}")
        return
    asset.compile_profile(profile, ppm=args.ppm, blend_px=args.blend_px,
                          crop_bottom=args.crop_bottom)
    stamp_file.write_text(stamp)


def build_inputs():
    """Every file a relink should follow: the C++ tree and the CMake scripts.

    "The executable exists" is not enough to call a build current, and the way it
    fails is silent. `write_config` emits whatever keys this Python knows, while
    the loader rejects any key its C++ does not — so a binary older than
    config.cpp fails at `unknown key '<newest key>'`, naming the config rather
    than the stale exe that cannot read it."""
    sources = [PROJECT_ROOT / "CMakeLists.txt"]
    sources += sorted((PROJECT_ROOT / "cmake").glob("*.cmake"))
    for suffix in ("cpp", "hpp", "h", "in", "mm", "metal", "hlsl"):
        sources += sorted((PROJECT_ROOT / "cpp").rglob(f"*.{suffix}"))
    return sources


def step_build(profile, args):        # noqa: ARG001 - signature parity
    build_dir = build_dir_for(args.backend)
    executable = executable_for(build_dir)
    if newer_than(executable, *build_inputs()) and not args.force:
        print(f"executable up to date: {executable}")
        copy_runtime_dlls(executable.parent)
        return
    if shutil.which("cmake") is None:
        raise StepError("cmake is not on PATH")
    configure = ["cmake", "-S", PROJECT_ROOT, "-B", build_dir,
                 f"-DPython3_EXECUTABLE={python_bin()}"]
    build = ["cmake", "--build", build_dir, "--target", "swim_realtime"]
    if is_windows():
        configure += ["-G", "Visual Studio 17 2022", "-A", "x64"]
        build += ["--config", "Release"]
    else:
        if shutil.which("ninja") is not None:
            configure += ["-G", "Ninja"]
        configure += ["-DCMAKE_BUILD_TYPE=Release"]
        build += ["-j", str(os.cpu_count() or 4)]
    run(configure)
    run(build)
    copy_runtime_dlls(executable_for(build_dir).parent)


def copy_runtime_dlls(destination):
    """Put FFmpeg / GLFW / cudart beside the Windows executable.

    CMake has no copy rules for them, and the exe links every backend that was
    available at configure time — so a tree without these DLLs fails to load with
    0xC0000135 whichever backend the run selects. No-op off Windows."""
    if not is_windows():
        return
    sources = []
    ffmpeg_bin = PROJECT_ROOT / "third_party" / "ffmpeg" / "bin"
    if ffmpeg_bin.is_dir():
        for library in ("avcodec", "avformat", "avutil", "swresample", "swscale"):
            sources += sorted(ffmpeg_bin.glob(f"{library}-*.dll"))
    glfw = PROJECT_ROOT / "third_party" / "glfw" / "lib-vc2022" / "glfw3.dll"
    if glfw.is_file():
        sources.append(glfw)
    cuda = Path(os.environ.get("CUDA_PATH")
                or "C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.8")
    cudart = cuda / "bin" / "cudart64_12.dll"
    if cudart.is_file():
        sources.append(cudart)

    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for source in sources:
        target = destination / source.name
        if not target.exists():
            shutil.copy2(source, target)
            copied += 1
    if not sources:
        print("  [warn] no third_party/CUDA DLLs found; run scripts\\install.bat")
    elif copied:
        print(f"  copied {copied} runtime DLL(s) beside the executable")


def write_config(profile, path, video_dir, backend, encode_path,
                 align=True, loop=True):
    """Emit a runtime config naming this line's lanes in order.

    Written fresh every run so the clip directory and backend always match what
    was asked for; the C++ loader takes camera identity straight from these
    `source.<id>` lines, in declaration order."""
    clips = {camera: profile.clip_for(video_dir, camera)
             for camera in profile.camera_ids}
    offsets = RV.lane_offsets_ms(profile, video_dir) if align else {}
    period = RV.loop_period_ms(profile, video_dir, offsets) if loop else 0
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
    return path


def step_live(profile, args):
    build_dir = build_dir_for(args.backend)
    executable = executable_for(build_dir)
    if not executable.is_file():
        raise StepError(f"executable missing (run the build step): {executable}")
    if not profile.asset.is_file():
        raise StepError(f"asset missing (run the asset step): {profile.asset}")

    encode_path = Path(args.encode_path or profile.encode_path)
    metrics = Path(args.metrics or profile.metrics)
    if args.config:
        config = Path(args.config)
        if not config.is_file():
            raise StepError(f"config does not exist: {config}")
    else:
        config = write_config(profile, profile.config_path(args.backend),
                              args.video_dir, args.backend, encode_path,
                              align=not args.no_align, loop=args.loop)

    # Loop controls are only in the config: repeating them here would silently
    # override whatever a caller-supplied --config asked for.
    command = [executable, "--config", config,
               f"--duration-seconds={args.seconds}",
               f"--preview={'true' if args.preview else 'false'}",
               f"--preview-visible={'true' if args.window else 'false'}",
               f"--metrics={metrics}"]
    if args.encode:
        encode_path.parent.mkdir(parents=True, exist_ok=True)
        command += ["--encode=true", "--encode-sink=file",
                    f"--encode-path={encode_path}"]
    if args.fps is not None:
        command += [f"--fps={args.fps}"]
    metrics.parent.mkdir(parents=True, exist_ok=True)
    run(command)
    print(f"metrics -> {metrics}")
    if args.encode:
        print(f"HEVC    -> {encode_path}")
