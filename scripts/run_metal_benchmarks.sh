#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$ROOT/configs/macos_20260629.conf"
BUILD_DIR="$ROOT/build/metal-release"
EXECUTABLE=""
OUTPUT_DIR=""
DURATION=15
VISIBLE=false
LIST_CELLS=false

usage() {
  cat <<'EOF'
Usage: scripts/run_metal_benchmarks.sh [options]
  --duration N       Seconds per cell (default: 15; values below 15 are non-publishable)
  --quick            One-second functional 48-cell smoke
  --visible          Use the AppKit preview window (default: offscreen Metal present sink)
  --config PATH      Runtime config
  --build-dir PATH   Release CMake build directory
  --executable PATH  Use an already-built Release executable
  --output-dir PATH  Result directory (default: benchmarks/runs/RUN_ID)
  --list-cells       Print the exact 48 cell identities without building or running
EOF
}

while (($#)); do
  case "$1" in
    --duration) DURATION="${2:?--duration requires N}"; shift 2 ;;
    --quick) DURATION=1; shift ;;
    --visible) VISIBLE=true; shift ;;
    --config) CONFIG="${2:?--config requires PATH}"; shift 2 ;;
    --build-dir) BUILD_DIR="${2:?--build-dir requires PATH}"; shift 2 ;;
    --executable) EXECUTABLE="${2:?--executable requires PATH}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:?--output-dir requires PATH}"; shift 2 ;;
    --list-cells) LIST_CELLS=true; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

STAGES=(decode-only render-only decode-render decode-render-preview decode-render-encode full)
COUNTS=(1 2 4 6)
PACINGS=(paced unpaced)

list_cells() {
  local stage count pacing
  for stage in "${STAGES[@]}"; do
    for count in "${COUNTS[@]}"; do
      for pacing in "${PACINGS[@]}"; do
        printf '%s,%s,%s\n' "$stage" "$count" "$pacing"
      done
    done
  done
}

if [[ "$LIST_CELLS" == true ]]; then
  list_cells
  exit 0
fi
if [[ ! "$DURATION" =~ ^[1-9][0-9]*$ ]]; then
  echo "--duration must be a positive integer" >&2
  exit 2
fi
PUBLISHABLE=false
if ((DURATION >= 15)); then PUBLISHABLE=true; fi
EXPECTED_GIT_SHA="$(git -C "$ROOT" rev-parse HEAD)"
SOURCE_DIRTY=false
if [[ -n "$(git -C "$ROOT" status --porcelain --untracked-files=normal)" ]]; then
  SOURCE_DIRTY=true
fi
if [[ "$PUBLISHABLE" == true && "$SOURCE_DIRTY" == true ]]; then
  echo "publishable benchmarks require a clean source tree" >&2
  exit 2
fi
if [[ ! -f "$CONFIG" ]]; then
  echo "config does not exist: $CONFIG" >&2
  exit 2
fi
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

if [[ -z "$EXECUTABLE" ]]; then
  cmake -S "$ROOT" -B "$BUILD_DIR" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DPython3_EXECUTABLE="$PYTHON"
  cmake --build "$BUILD_DIR" --target swim_realtime
  EXECUTABLE="$BUILD_DIR/swim_realtime"
fi
if [[ ! -x "$EXECUTABLE" ]]; then
  echo "Release executable is not executable: $EXECUTABLE" >&2
  exit 2
fi
EXECUTABLE="$(cd "$(dirname "$EXECUTABLE")" && pwd -P)/$(basename "$EXECUTABLE")"

config_value() {
  local key="$1"
  awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 1}' "$CONFIG"
}
resolve_path() {
  local value="$1"
  if [[ "$value" = /* ]]; then printf '%s\n' "$value"; else printf '%s/%s\n' "$ROOT" "$value"; fi
}
sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    sha256sum "$1" | awk '{print $1}'
  fi
}

ASSET="$(resolve_path "$(config_value asset)")"
CAMERAS=(cam3 cam2 cam1 cam4 cam5 cam6)
SOURCES=()
for camera in "${CAMERAS[@]}"; do
  SOURCES+=("$(resolve_path "$(config_value "source.$camera")")")
done
for path in "$ASSET" "${SOURCES[@]}"; do
  if [[ ! -f "$path" ]]; then echo "benchmark input does not exist: $path" >&2; exit 2; fi
done

# Fingerprints are intentionally computed once and reused by all 48 cells.
ASSET_SHA="$(sha256_file "$ASSET")"
SOURCE_SHAS=()
for path in "${SOURCES[@]}"; do SOURCE_SHAS+=("$(sha256_file "$path")"); done

RUN_ID="metal-$(date -u +%Y%m%dT%H%M%SZ)-$$"
if [[ -z "$OUTPUT_DIR" ]]; then OUTPUT_DIR="$ROOT/benchmarks/runs/$RUN_ID"; fi
mkdir -p "$OUTPUT_DIR/cells" "$OUTPUT_DIR/logs"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd -P)"
RUNTIME_MANIFEST="$OUTPUT_DIR/runtime.manifest"
{
  printf 'run_id=%s\n' "$RUN_ID"
  printf 'asset_sha256=%s\n' "$ASSET_SHA"
  for index in 0 1 2 3 4 5; do
    printf 'source.%s_sha256=%s\n' "${CAMERAS[$index]}" "${SOURCE_SHAS[$index]}"
  done
} >"$RUNTIME_MANIFEST"

# A one-second real render-only cell obtains identity from this exact binary
# before any of the 48 measured cells can run.
PREFLIGHT_METRICS="$OUTPUT_DIR/preflight.jsonl"
rm -f "$PREFLIGHT_METRICS"
"$EXECUTABLE" --config "$CONFIG" --stage=render-only --stream-count=1 \
  --mode=realtime --duration-seconds=1 --preview=true \
  "--preview-visible=$VISIBLE" --encode=true --encode-sink=null \
  "--benchmark-manifest=$RUNTIME_MANIFEST" "--metrics=$PREFLIGHT_METRICS" \
  >"$OUTPUT_DIR/logs/preflight.log" 2>&1
"$PYTHON" -m python.validation.summarize_benchmarks "$PREFLIGHT_METRICS" \
  --cell-only --expected-stage render-only --expected-stream-count 1 \
  --expected-pacing paced --expected-git-sha "$EXPECTED_GIT_SHA" \
  --expected-build-type Release
EMBEDDED_SHA="$EXPECTED_GIT_SHA"
EMBEDDED_BUILD_TYPE=Release
EXECUTABLE_SHA="$(sha256_file "$EXECUTABLE")"

"$PYTHON" - "$OUTPUT_DIR/manifest.json" "$RUN_ID" "$DURATION" "$PUBLISHABLE" "$VISIBLE" "$EMBEDDED_SHA" "$EMBEDDED_BUILD_TYPE" "$EXECUTABLE_SHA" "$SOURCE_DIRTY" "$ASSET_SHA" "${SOURCE_SHAS[@]}" <<'PY'
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

echo "run_id=$RUN_ID duration=$DURATION publishable=$PUBLISHABLE visible=$VISIBLE"
cell_count=0
for stage in "${STAGES[@]}"; do
  for count in "${COUNTS[@]}"; do
    for pacing in "${PACINGS[@]}"; do
      mode=benchmark
      if [[ "$pacing" == paced ]]; then mode=realtime; fi
      cell="$stage-$count-$pacing"
      metrics="$OUTPUT_DIR/cells/$cell.jsonl"
      log="$OUTPUT_DIR/logs/$cell.log"
      rm -f "$metrics"
      echo "[$((cell_count + 1))/48] $stage streams=$count pacing=$pacing"
      "$EXECUTABLE" --config "$CONFIG" \
        "--stage=$stage" "--stream-count=$count" "--mode=$mode" \
        "--duration-seconds=$DURATION" --preview=true \
        "--preview-visible=$VISIBLE" --encode=true --encode-sink=null \
        "--benchmark-manifest=$RUNTIME_MANIFEST" "--metrics=$metrics" \
        >"$log" 2>&1
      "$PYTHON" -m python.validation.summarize_benchmarks "$metrics" \
        --cell-only --expected-stage "$stage" --expected-stream-count "$count" \
        --expected-pacing "$pacing" --expected-git-sha "$EMBEDDED_SHA" \
        --expected-build-type "$EMBEDDED_BUILD_TYPE"
      ((cell_count += 1))
    done
  done
done

RESULTS="$OUTPUT_DIR/results.jsonl"
: >"$RESULTS"
while IFS=, read -r stage count pacing; do
  cat "$OUTPUT_DIR/cells/$stage-$count-$pacing.jsonl" >>"$RESULTS"
done < <(list_cells)

SUMMARY_ARGS=("$RESULTS" --csv "$OUTPUT_DIR/summary.csv" --markdown "$OUTPUT_DIR/summary.md")
if [[ "$PUBLISHABLE" == true ]]; then SUMMARY_ARGS+=(--publishable); fi
"$PYTHON" -m python.validation.summarize_benchmarks "${SUMMARY_ARGS[@]}"

mkdir -p "$ROOT/benchmarks"
ln -sfn "$OUTPUT_DIR" "$ROOT/benchmarks/latest"
echo "benchmark matrix complete: $OUTPUT_DIR"
