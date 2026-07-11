#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/configs/macos_20260629.conf"
BUILD_DIR="$ROOT/build/metal-release"
EXECUTABLE=""
OUTPUT_DIR=""
DURATION=600
WARMUP=30
MIN_FPS=29.0
VISIBLE=false
MAX_RSS_SLOPE=67108864
MAX_GPU_SLOPE=33554432

usage() {
  cat <<'EOF'
Usage: scripts/run_metal_soak.sh [options]
  --duration N       Soak seconds (default: 600)
  --warmup N         Warm-up intervals ignored by FPS/slope gates (default: 30)
  --min-fps N        Fail after five sustained post-warmup intervals (default: 29.0)
  --max-rss-slope N  Maximum RSS growth bytes/minute (default: 67108864)
  --max-gpu-slope N  Maximum Metal allocation growth bytes/minute (default: 33554432)
  --visible          Use the AppKit preview window (default: offscreen Metal sink)
  --config PATH      Runtime config
  --build-dir PATH   Release CMake build directory
  --executable PATH  Use an already-built Release executable
  --output-dir PATH  Result directory
EOF
}

while (($#)); do
  case "$1" in
    --duration) DURATION="${2:?--duration requires N}"; shift 2 ;;
    --warmup) WARMUP="${2:?--warmup requires N}"; shift 2 ;;
    --min-fps) MIN_FPS="${2:?--min-fps requires N}"; shift 2 ;;
    --max-rss-slope) MAX_RSS_SLOPE="${2:?--max-rss-slope requires N}"; shift 2 ;;
    --max-gpu-slope) MAX_GPU_SLOPE="${2:?--max-gpu-slope requires N}"; shift 2 ;;
    --visible) VISIBLE=true; shift ;;
    --config) CONFIG="${2:?--config requires PATH}"; shift 2 ;;
    --build-dir) BUILD_DIR="${2:?--build-dir requires PATH}"; shift 2 ;;
    --executable) EXECUTABLE="${2:?--executable requires PATH}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:?--output-dir requires PATH}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done
if [[ ! "$DURATION" =~ ^[1-9][0-9]*$ || ! "$WARMUP" =~ ^[0-9]+$ ]]; then
  echo "duration must be positive and warmup nonnegative integers" >&2; exit 2
fi
PYTHON="$ROOT/.venv/bin/python"; [[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"
EXPECTED_GIT_SHA="$(git -C "$ROOT" rev-parse HEAD)"
if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]]; then
  echo "soak evidence requires a clean source tree" >&2
  exit 2
fi
if [[ -z "$EXECUTABLE" ]]; then
  cmake -S "$ROOT" -B "$BUILD_DIR" -G Ninja -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE="$PYTHON"
  cmake --build "$BUILD_DIR" --target swim_realtime
  EXECUTABLE="$BUILD_DIR/swim_realtime"
fi
[[ -x "$EXECUTABLE" ]] || { echo "Release executable is not executable: $EXECUTABLE" >&2; exit 2; }
EXECUTABLE="$(cd "$(dirname "$EXECUTABLE")" && pwd -P)/$(basename "$EXECUTABLE")"

config_value() { awk -F= -v wanted="$1" '$1 == wanted {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 1}' "$CONFIG"; }
resolve_path() { if [[ "$1" = /* ]]; then printf '%s\n' "$1"; else printf '%s/%s\n' "$ROOT" "$1"; fi; }
sha256_file() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'; else sha256sum "$1" | awk '{print $1}'; fi; }

ASSET="$(resolve_path "$(config_value asset)")"
CAMERAS=(cam3 cam2 cam1 cam4 cam5 cam6)
SOURCES=(); for camera in "${CAMERAS[@]}"; do SOURCES+=("$(resolve_path "$(config_value "source.$camera")")"); done
for path in "$ASSET" "${SOURCES[@]}"; do [[ -f "$path" ]] || { echo "soak input does not exist: $path" >&2; exit 2; }; done
ASSET_SHA="$(sha256_file "$ASSET")"
SOURCE_SHAS=(); for path in "${SOURCES[@]}"; do SOURCE_SHAS+=("$(sha256_file "$path")"); done
RUN_ID="metal-soak-$(date -u +%Y%m%dT%H%M%SZ)-$$"
[[ -n "$OUTPUT_DIR" ]] || OUTPUT_DIR="$ROOT/benchmarks/soaks/$RUN_ID"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)"
MANIFEST="$OUTPUT_DIR/runtime.manifest"
{
  printf 'run_id=%s\nasset_sha256=%s\n' "$RUN_ID" "$ASSET_SHA"
  for index in 0 1 2 3 4 5; do printf 'source.%s_sha256=%s\n' "${CAMERAS[$index]}" "${SOURCE_SHAS[$index]}"; done
} >"$MANIFEST"
PREFLIGHT="$OUTPUT_DIR/preflight.jsonl"; rm -f "$PREFLIGHT"
"$EXECUTABLE" --config "$CONFIG" --stage=render-only --stream-count=1 --mode=realtime \
  --duration-seconds=1 --preview=true "--preview-visible=$VISIBLE" \
  --encode=true --encode-sink=null "--benchmark-manifest=$MANIFEST" "--metrics=$PREFLIGHT" \
  >"$OUTPUT_DIR/preflight.log" 2>&1
"$PYTHON" -m python.validation.summarize_benchmarks "$PREFLIGHT" --cell-only \
  --expected-stage render-only --expected-stream-count 1 --expected-pacing paced \
  --expected-git-sha "$EXPECTED_GIT_SHA" --expected-build-type Release
EXECUTABLE_SHA="$(sha256_file "$EXECUTABLE")"
"$PYTHON" - "$OUTPUT_DIR/manifest.json" "$RUN_ID" "$EXPECTED_GIT_SHA" "$EXECUTABLE_SHA" "$ASSET_SHA" "${SOURCE_SHAS[@]}" <<'PY'
import json, sys
path, run_id, git_sha, executable_sha, asset_sha, *source_shas = sys.argv[1:]
with open(path, "w", encoding="utf-8") as output:
    json.dump({"run_id": run_id, "git_sha": git_sha, "build_type": "Release",
               "executable_sha256": executable_sha, "source_dirty": False,
               "asset_sha256": asset_sha, "source_sha256": source_shas},
              output, indent=2, sort_keys=True)
    output.write("\n")
PY
METRICS="$OUTPUT_DIR/results.jsonl"; rm -f "$METRICS"
"$EXECUTABLE" --config "$CONFIG" --stage=full --stream-count=6 --mode=realtime \
  "--duration-seconds=$DURATION" --preview=true "--preview-visible=$VISIBLE" \
  --encode=true --encode-sink=null "--benchmark-manifest=$MANIFEST" "--metrics=$METRICS" \
  >"$OUTPUT_DIR/runtime.log" 2>&1
"$PYTHON" -m python.validation.summarize_benchmarks "$METRICS" --cell-only \
  --expected-stage full --expected-stream-count 6 --expected-pacing paced \
  --expected-git-sha "$EXPECTED_GIT_SHA" --expected-build-type Release
SOAK_ARGS=("$METRICS" --soak-only --warmup-seconds "$WARMUP" --min-fps "$MIN_FPS"
  --max-rss-slope-bytes-per-minute "$MAX_RSS_SLOPE"
  --max-gpu-slope-bytes-per-minute "$MAX_GPU_SLOPE")
"$PYTHON" -m python.validation.summarize_benchmarks "${SOAK_ARGS[@]}" | tee "$OUTPUT_DIR/soak-summary.json"
echo "Metal soak complete: $OUTPUT_DIR"
