#!/usr/bin/env bash
# Metal 实时路径统一入口：可视化 demo、性能矩阵、soak。
#
# 用法:
#   ./scripts/run_metal.sh demo [--duration N] [--no-window] [--no-encode] [...]
#   ./scripts/run_metal.sh benchmarks [--quick] [--duration N] [--visible] [...]
#   ./scripts/run_metal.sh soak [--duration N] [--visible] [...]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/configs/macos_20260629.conf"
BUILD_DIR="$ROOT/build/metal-release"
EXECUTABLE=""
OUTPUT_DIR=""
VISIBLE=false

usage() {
  cat <<'EOF'
Usage: scripts/run_metal.sh <command> [options]

Commands:
  demo         Six-camera realtime stitch with AppKit preview window
  benchmarks   Release 48-cell performance matrix
  soak         Paced full-stage soak with leak/FPS gates

demo options:
  --duration N       Seconds (default: 30)
  --no-window        Offscreen Metal present sink (no AppKit window)
  --no-encode        Skip HEVC file write (encode-sink=null)
  --encode-path PATH HEVC output (default: outputs/videos/pool_metal.h265)
  --metrics PATH     Metrics JSONL (default: benchmarks/manual.jsonl)
  --config PATH      Runtime config
  --build-dir PATH   Release CMake build directory
  --executable PATH  Use an already-built Release executable
  --stage NAME       Runtime stage (default: full)
  --stream-count N   Active camera count (default: 6)

benchmarks options:
  --duration N       Seconds per cell (default: 15; <15 is non-publishable)
  --quick            One-second functional 48-cell smoke
  --visible          Use the AppKit preview window (default: offscreen)
  --config PATH      Runtime config
  --build-dir PATH   Release CMake build directory
  --executable PATH  Use an already-built Release executable
  --output-dir PATH  Result directory (default: benchmarks/runs/RUN_ID)
  --list-cells       Print the exact 48 cell identities without building or running

soak options:
  --duration N       Soak seconds (default: 600)
  --warmup N         Warm-up intervals ignored by FPS/slope gates (default: 30)
  --min-fps N        Fail after five sustained post-warmup intervals (default: 29.0)
  --max-rss-slope N  Maximum RSS growth bytes/minute (default: 67108864)
  --max-gpu-slope N  Maximum Metal allocation growth bytes/minute (default: 33554432)
  --visible          Use the AppKit preview window (default: offscreen)
  --config PATH      Runtime config
  --build-dir PATH   Release CMake build directory
  --executable PATH  Use an already-built Release executable
  --output-dir PATH  Result directory
EOF
}

python_bin() {
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    printf '%s\n' "$ROOT/.venv/bin/python"
  else
    command -v python3
  fi
}

ensure_executable() {
  local python
  python="$(python_bin)"
  if [[ -z "$EXECUTABLE" ]]; then
    cmake -S "$ROOT" -B "$BUILD_DIR" -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE="$python"
    cmake --build "$BUILD_DIR" --target swim_realtime
    EXECUTABLE="$BUILD_DIR/swim_realtime"
  fi
  if [[ ! -x "$EXECUTABLE" ]]; then
    echo "Release executable is not executable: $EXECUTABLE" >&2
    exit 2
  fi
  EXECUTABLE="$(cd "$(dirname "$EXECUTABLE")" && pwd -P)/$(basename "$EXECUTABLE")"
}

config_value() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 1}' "$CONFIG"
}

resolve_path() {
  local value="$1"
  if [[ "$value" = /* ]]; then
    printf '%s\n' "$value"
  else
    printf '%s/%s\n' "$ROOT" "$value"
  fi
}

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

cmd_demo() {
  local duration=30
  local window=true
  local encode=true
  local encode_path="$ROOT/outputs/videos/pool_metal.h265"
  local metrics="$ROOT/benchmarks/manual.jsonl"
  local stage=full
  local stream_count=6

  while (($#)); do
    case "$1" in
      --duration) duration="${2:?--duration requires N}"; shift 2 ;;
      --no-window) window=false; shift ;;
      --no-encode) encode=false; shift ;;
      --encode-path) encode_path="${2:?--encode-path requires PATH}"; shift 2 ;;
      --metrics) metrics="${2:?--metrics requires PATH}"; shift 2 ;;
      --config) CONFIG="${2:?--config requires PATH}"; shift 2 ;;
      --build-dir) BUILD_DIR="${2:?--build-dir requires PATH}"; shift 2 ;;
      --executable) EXECUTABLE="${2:?--executable requires PATH}"; shift 2 ;;
      --stage) stage="${2:?--stage requires NAME}"; shift 2 ;;
      --stream-count) stream_count="${2:?--stream-count requires N}"; shift 2 ;;
      --help|-h) usage; exit 0 ;;
      *) echo "unknown demo option: $1" >&2; usage >&2; exit 2 ;;
    esac
  done

  if [[ ! "$duration" =~ ^[1-9][0-9]*$ ]]; then
    echo "--duration must be a positive integer" >&2
    exit 2
  fi
  if [[ ! -f "$CONFIG" ]]; then
    echo "config does not exist: $CONFIG" >&2
    exit 2
  fi

  ensure_executable
  mkdir -p "$(dirname "$metrics")"
  if [[ "$encode" == true ]]; then
    mkdir -p "$(dirname "$encode_path")"
  fi

  local -a args=(
    --config "$CONFIG"
    "--stage=$stage"
    "--stream-count=$stream_count"
    --mode=realtime
    "--duration-seconds=$duration"
    --preview=true
    "--preview-visible=$window"
  )
  if [[ "$encode" == true ]]; then
    args+=(--encode=true --encode-sink=file "--encode-path=$encode_path")
  else
    args+=(--encode=true --encode-sink=null)
  fi
  args+=("--metrics=$metrics")

  echo "Metal demo: window=$window encode=$encode duration=${duration}s"
  echo "  executable: $EXECUTABLE"
  if [[ "$encode" == true ]]; then
    echo "  hevc: $encode_path"
  fi
  echo "  metrics: $metrics"
  "$EXECUTABLE" "${args[@]}"
  echo "Metal demo complete."
  if [[ "$encode" == true ]]; then
    echo "HEVC output -> $encode_path"
  fi
  echo "metrics -> $metrics"
}

cmd_benchmarks() {
  local duration=15
  local list_cells=false
  VISIBLE=false
  OUTPUT_DIR=""

  while (($#)); do
    case "$1" in
      --duration) duration="${2:?--duration requires N}"; shift 2 ;;
      --quick) duration=1; shift ;;
      --visible) VISIBLE=true; shift ;;
      --config) CONFIG="${2:?--config requires PATH}"; shift 2 ;;
      --build-dir) BUILD_DIR="${2:?--build-dir requires PATH}"; shift 2 ;;
      --executable) EXECUTABLE="${2:?--executable requires PATH}"; shift 2 ;;
      --output-dir) OUTPUT_DIR="${2:?--output-dir requires PATH}"; shift 2 ;;
      --list-cells) list_cells=true; shift ;;
      --help|-h) usage; exit 0 ;;
      *) echo "unknown benchmarks option: $1" >&2; usage >&2; exit 2 ;;
    esac
  done

  local -a stages=(decode-only render-only decode-render decode-render-preview decode-render-encode full)
  local -a counts=(1 2 4 6)
  local -a pacings=(paced unpaced)

  list_cells_fn() {
    local stage count pacing
    for stage in "${stages[@]}"; do
      for count in "${counts[@]}"; do
        for pacing in "${pacings[@]}"; do
          printf '%s,%s,%s\n' "$stage" "$count" "$pacing"
        done
      done
    done
  }

  if [[ "$list_cells" == true ]]; then
    list_cells_fn
    exit 0
  fi
  if [[ ! "$duration" =~ ^[1-9][0-9]*$ ]]; then
    echo "--duration must be a positive integer" >&2
    exit 2
  fi

  local publishable=false
  if ((duration >= 15)); then publishable=true; fi
  local expected_git_sha
  expected_git_sha="$(git -C "$ROOT" rev-parse HEAD)"
  local source_dirty=false
  if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]]; then
    source_dirty=true
  fi
  if [[ "$publishable" == true && "$source_dirty" == true ]]; then
    echo "publishable benchmarks require a clean source tree" >&2
    exit 2
  fi
  if [[ ! -f "$CONFIG" ]]; then
    echo "config does not exist: $CONFIG" >&2
    exit 2
  fi

  local python
  python="$(python_bin)"
  ensure_executable

  local asset
  asset="$(resolve_path "$(config_value asset)")"
  local -a cameras=(cam3 cam2 cam1 cam4 cam5 cam6)
  local -a sources=()
  local camera
  for camera in "${cameras[@]}"; do
    sources+=("$(resolve_path "$(config_value "source.$camera")")")
  done
  local path
  for path in "$asset" "${sources[@]}"; do
    if [[ ! -f "$path" ]]; then
      echo "benchmark input does not exist: $path" >&2
      exit 2
    fi
  done

  local asset_sha
  asset_sha="$(sha256_file "$asset")"
  local -a source_shas=()
  for path in "${sources[@]}"; do
    source_shas+=("$(sha256_file "$path")")
  done

  local run_id="metal-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  if [[ -z "$OUTPUT_DIR" ]]; then
    OUTPUT_DIR="$ROOT/benchmarks/runs/$run_id"
  fi
  mkdir -p "$OUTPUT_DIR/cells" "$OUTPUT_DIR/logs"
  OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)"
  local runtime_manifest="$OUTPUT_DIR/runtime.manifest"
  {
    printf 'run_id=%s\n' "$run_id"
    printf 'asset_sha256=%s\n' "$asset_sha"
    local index
    for index in 0 1 2 3 4 5; do
      printf 'source.%s_sha256=%s\n' "${cameras[$index]}" "${source_shas[$index]}"
    done
  } >"$runtime_manifest"

  local preflight_metrics="$OUTPUT_DIR/preflight.jsonl"
  rm -f "$preflight_metrics"
  "$EXECUTABLE" --config "$CONFIG" --stage=render-only --stream-count=1 \
    --mode=realtime --duration-seconds=1 --preview=true \
    "--preview-visible=$VISIBLE" --encode=true --encode-sink=null \
    "--benchmark-manifest=$runtime_manifest" "--metrics=$preflight_metrics" \
    >"$OUTPUT_DIR/logs/preflight.log" 2>&1
  "$python" -m python.validation.summarize_benchmarks "$preflight_metrics" \
    --cell-only --expected-stage render-only --expected-stream-count 1 \
    --expected-pacing paced --expected-git-sha "$expected_git_sha" \
    --expected-build-type Release
  local embedded_sha="$expected_git_sha"
  local embedded_build_type=Release
  local executable_sha
  executable_sha="$(sha256_file "$EXECUTABLE")"

  "$python" - "$OUTPUT_DIR/manifest.json" "$run_id" "$duration" "$publishable" "$VISIBLE" \
    "$embedded_sha" "$embedded_build_type" "$executable_sha" "$source_dirty" "$asset_sha" \
    "${source_shas[@]}" <<'PY'
import json, sys
path, run_id, duration, publishable, visible, git_sha, build_type, executable_sha, source_dirty, asset_sha, *source_shas = sys.argv[1:]
payload = {
    "run_id": run_id, "duration_seconds": int(duration),
    "publishable": publishable == "true", "visible_preview": visible == "true",
    "expected_cells": 48, "git_sha": git_sha, "build_type": build_type,
    "executable_sha256": executable_sha, "source_dirty": source_dirty == "true",
    "asset_sha256": asset_sha, "source_sha256": source_shas,
}
with open(path, "w", encoding="utf-8") as output:
    json.dump(payload, output, indent=2, sort_keys=True)
    output.write("\n")
PY

  echo "run_id=$run_id duration=$duration publishable=$publishable visible=$VISIBLE"
  local cell_count=0
  local stage count pacing mode cell metrics log
  for stage in "${stages[@]}"; do
    for count in "${counts[@]}"; do
      for pacing in "${pacings[@]}"; do
        mode=benchmark
        if [[ "$pacing" == paced ]]; then mode=realtime; fi
        cell="$stage-$count-$pacing"
        metrics="$OUTPUT_DIR/cells/$cell.jsonl"
        log="$OUTPUT_DIR/logs/$cell.log"
        rm -f "$metrics"
        echo "[$((cell_count + 1))/48] $stage streams=$count pacing=$pacing"
        "$EXECUTABLE" --config "$CONFIG" \
          "--stage=$stage" "--stream-count=$count" "--mode=$mode" \
          "--duration-seconds=$duration" --preview=true \
          "--preview-visible=$VISIBLE" --encode=true --encode-sink=null \
          "--benchmark-manifest=$runtime_manifest" "--metrics=$metrics" \
          >"$log" 2>&1
        "$python" -m python.validation.summarize_benchmarks "$metrics" \
          --cell-only --expected-stage "$stage" --expected-stream-count "$count" \
          --expected-pacing "$pacing" --expected-git-sha "$embedded_sha" \
          --expected-build-type "$embedded_build_type"
        ((cell_count += 1))
      done
    done
  done

  local results="$OUTPUT_DIR/results.jsonl"
  : >"$results"
  while IFS=, read -r stage count pacing; do
    cat "$OUTPUT_DIR/cells/$stage-$count-$pacing.jsonl" >>"$results"
  done < <(list_cells_fn)

  local -a summary_args=("$results" --csv "$OUTPUT_DIR/summary.csv" --markdown "$OUTPUT_DIR/summary.md")
  if [[ "$publishable" == true ]]; then
    summary_args+=(--publishable)
  fi
  "$python" -m python.validation.summarize_benchmarks "${summary_args[@]}"

  mkdir -p "$ROOT/benchmarks"
  ln -sfn "$OUTPUT_DIR" "$ROOT/benchmarks/latest"
  echo "benchmark matrix complete: $OUTPUT_DIR"
}

cmd_soak() {
  local duration=600
  local warmup=30
  local min_fps=29.0
  local max_rss_slope=67108864
  local max_gpu_slope=33554432
  VISIBLE=false
  OUTPUT_DIR=""

  while (($#)); do
    case "$1" in
      --duration) duration="${2:?--duration requires N}"; shift 2 ;;
      --warmup) warmup="${2:?--warmup requires N}"; shift 2 ;;
      --min-fps) min_fps="${2:?--min-fps requires N}"; shift 2 ;;
      --max-rss-slope) max_rss_slope="${2:?--max-rss-slope requires N}"; shift 2 ;;
      --max-gpu-slope) max_gpu_slope="${2:?--max-gpu-slope requires N}"; shift 2 ;;
      --visible) VISIBLE=true; shift ;;
      --config) CONFIG="${2:?--config requires PATH}"; shift 2 ;;
      --build-dir) BUILD_DIR="${2:?--build-dir requires PATH}"; shift 2 ;;
      --executable) EXECUTABLE="${2:?--executable requires PATH}"; shift 2 ;;
      --output-dir) OUTPUT_DIR="${2:?--output-dir requires PATH}"; shift 2 ;;
      --help|-h) usage; exit 0 ;;
      *) echo "unknown soak option: $1" >&2; usage >&2; exit 2 ;;
    esac
  done

  if [[ ! "$duration" =~ ^[1-9][0-9]*$ || ! "$warmup" =~ ^[0-9]+$ ]]; then
    echo "duration must be positive and warmup nonnegative integers" >&2
    exit 2
  fi

  local python
  python="$(python_bin)"
  local expected_git_sha
  expected_git_sha="$(git -C "$ROOT" rev-parse HEAD)"
  if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]]; then
    echo "soak evidence requires a clean source tree" >&2
    exit 2
  fi

  ensure_executable

  local asset
  asset="$(resolve_path "$(config_value asset)")"
  local -a cameras=(cam3 cam2 cam1 cam4 cam5 cam6)
  local -a sources=()
  local camera
  for camera in "${cameras[@]}"; do
    sources+=("$(resolve_path "$(config_value "source.$camera")")")
  done
  local path
  for path in "$asset" "${sources[@]}"; do
    [[ -f "$path" ]] || { echo "soak input does not exist: $path" >&2; exit 2; }
  done
  local asset_sha
  asset_sha="$(sha256_file "$asset")"
  local -a source_shas=()
  for path in "${sources[@]}"; do
    source_shas+=("$(sha256_file "$path")")
  done

  local run_id="metal-soak-$(date -u +%Y%m%dT%H%M%SZ)-$$"
  [[ -n "$OUTPUT_DIR" ]] || OUTPUT_DIR="$ROOT/benchmarks/soaks/$run_id"
  mkdir -p "$OUTPUT_DIR"
  OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)"
  local manifest="$OUTPUT_DIR/runtime.manifest"
  {
    printf 'run_id=%s\nasset_sha256=%s\n' "$run_id" "$asset_sha"
    local index
    for index in 0 1 2 3 4 5; do
      printf 'source.%s_sha256=%s\n' "${cameras[$index]}" "${source_shas[$index]}"
    done
  } >"$manifest"

  local preflight="$OUTPUT_DIR/preflight.jsonl"
  rm -f "$preflight"
  "$EXECUTABLE" --config "$CONFIG" --stage=render-only --stream-count=1 --mode=realtime \
    --duration-seconds=1 --preview=true "--preview-visible=$VISIBLE" \
    --encode=true --encode-sink=null "--benchmark-manifest=$manifest" "--metrics=$preflight" \
    >"$OUTPUT_DIR/preflight.log" 2>&1
  "$python" -m python.validation.summarize_benchmarks "$preflight" --cell-only \
    --expected-stage render-only --expected-stream-count 1 --expected-pacing paced \
    --expected-git-sha "$expected_git_sha" --expected-build-type Release
  local executable_sha
  executable_sha="$(sha256_file "$EXECUTABLE")"
  "$python" - "$OUTPUT_DIR/manifest.json" "$run_id" "$expected_git_sha" "$executable_sha" \
    "$asset_sha" "${source_shas[@]}" <<'PY'
import json, sys
path, run_id, git_sha, executable_sha, asset_sha, *source_shas = sys.argv[1:]
with open(path, "w", encoding="utf-8") as output:
    json.dump({"run_id": run_id, "git_sha": git_sha, "build_type": "Release",
               "executable_sha256": executable_sha, "source_dirty": False,
               "asset_sha256": asset_sha, "source_sha256": source_shas},
              output, indent=2, sort_keys=True)
    output.write("\n")
PY

  local metrics="$OUTPUT_DIR/results.jsonl"
  rm -f "$metrics"
  "$EXECUTABLE" --config "$CONFIG" --stage=full --stream-count=6 --mode=realtime \
    "--duration-seconds=$duration" --preview=true "--preview-visible=$VISIBLE" \
    --encode=true --encode-sink=null "--benchmark-manifest=$manifest" "--metrics=$metrics" \
    >"$OUTPUT_DIR/runtime.log" 2>&1
  "$python" -m python.validation.summarize_benchmarks "$metrics" --cell-only \
    --expected-stage full --expected-stream-count 6 --expected-pacing paced \
    --expected-git-sha "$expected_git_sha" --expected-build-type Release
  local -a soak_args=(
    "$metrics" --soak-only --warmup-seconds "$warmup" --min-fps "$min_fps"
    --max-rss-slope-bytes-per-minute "$max_rss_slope"
    --max-gpu-slope-bytes-per-minute "$max_gpu_slope"
  )
  "$python" -m python.validation.summarize_benchmarks "${soak_args[@]}" | tee "$OUTPUT_DIR/soak-summary.json"
  echo "Metal soak complete: $OUTPUT_DIR"
}

if (($# == 0)); then
  usage >&2
  exit 2
fi

COMMAND="$1"
shift
case "$COMMAND" in
  demo) cmd_demo "$@" ;;
  benchmarks|bench|matrix) cmd_benchmarks "$@" ;;
  soak) cmd_soak "$@" ;;
  --help|-h|help) usage ;;
  *)
    echo "unknown command: $COMMAND" >&2
    usage >&2
    exit 2
    ;;
esac
