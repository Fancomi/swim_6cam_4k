#!/usr/bin/env bash
# Python 离线应用统一入口：静图/4K 拼接、关键点预览、FBX 提取与运行时资产。
#
# 用法:
#   ./scripts/run_python.sh still
#   ./scripts/run_python.sh 4k [SECONDS] [OUTPUT_MP4]
#   ./scripts/run_python.sh keypoint [--dataset-root PATH] [...]
#   ./scripts/run_python.sh extract [SRC_FBX] [DST_JSON]
#   ./scripts/run_python.sh bake SRC_FBX DST_FBX [--ext-px N]
#   ./scripts/run_python.sh asset [INPUT_JSON] [OUTPUT_SWASSET]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

DATASET_DIR="${SWIMMING_DATASET_DIR:-/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K}"
SESSION="20260629_172532"

usage() {
  cat <<'EOF'
Usage: scripts/run_python.sh <command> [args]

Commands:
  still      Render static stitch PNG from composite textures
  4k         Render 4K six-camera H.264 stitch (default 10s test clip)
  keypoint   Build COCO-17 person-crop HTML review page
  extract    Extract pool FBX mesh JSON
  bake       Bake centre-line UV extension into a new FBX
  asset      Compile mesh JSON into GPU runtime .swasset

Examples:
  ./scripts/run_python.sh still
  ./scripts/run_python.sh 4k
  ./scripts/run_python.sh 4k 30
  ./scripts/run_python.sh 4k 602 outputs/videos/pool_4k_full.mp4
  ./scripts/run_python.sh keypoint
  ./scripts/run_python.sh extract
  ./scripts/run_python.sh asset
EOF
}

cmd_still() {
  local still="$ROOT/outputs/images/pool.png"
  mkdir -p "$(dirname "$still")"
  "$PY" -m python.validation.reference_renderer \
    --data "$ROOT/outputs/data/pool_mesh.json" \
    --tex-dir "$ROOT/inputs/pool/textures" \
    --still "$still"
  echo "done -> $still"
}

cmd_4k() {
  local seconds_arg="${1:-10}"
  local out="${2:-$ROOT/outputs/videos/pool_4k_test${seconds_arg}s.mp4}"
  local -a videos=(
    "$DATASET_DIR/${SESSION}_cam3.mp4"
    "$DATASET_DIR/${SESSION}_cam2.mp4"
    "$DATASET_DIR/${SESSION}_cam1.mp4"
    "$DATASET_DIR/${SESSION}_cam4.mp4"
    "$DATASET_DIR/${SESSION}_cam5.mp4"
    "$DATASET_DIR/${SESSION}_cam6.mp4"
  )

  [[ -d "$DATASET_DIR" ]] || {
    echo "dataset directory not found: $DATASET_DIR" >&2
    exit 1
  }
  local video
  for video in "${videos[@]}"; do
    [[ -f "$video" ]] || {
      echo "video not found: $video" >&2
      exit 1
    }
  done

  mkdir -p "$(dirname "$out")"
  "$PY" -m python.validation.reference_renderer \
    --data "$ROOT/outputs/data/pool_mesh.json" \
    --videos "${videos[@]}" \
    --video "$out" \
    --seconds "$seconds_arg"
  echo "done -> $out"
}

cmd_keypoint() {
  "$PY" -m python.assets.build_keypoint_preview "$@"
  echo "open outputs/keypoint_preview/index.html (or --output-dir) in a browser"
}

cmd_extract() {
  if (($# == 0)); then
    "$PY" -m python.assets.extract_fbx
  else
    "$PY" -m python.assets.extract_fbx "$@"
  fi
}

cmd_bake() {
  if (($# < 2)); then
    echo "usage: scripts/run_python.sh bake SRC_FBX DST_FBX [--ext-px N] [--tex-dir DIR]" >&2
    exit 2
  fi
  "$PY" -m python.assets.bake_uv "$@"
}

cmd_asset() {
  local input_json="${1:-$ROOT/outputs/data/pool_mesh.json}"
  local output_swasset="${2:-$ROOT/assets/generated/pool_4k.swasset}"
  shift $(( $# >= 2 ? 2 : $# )) || true
  mkdir -p "$(dirname "$output_swasset")"
  "$PY" -m python.assets.compile_runtime_asset \
    "$input_json" "$output_swasset" \
    --camera-ids cam3 cam2 cam1 cam4 cam5 cam6 --ppm 100 \
    "$@"
  echo "done -> $output_swasset"
}

if (($# == 0)); then
  usage >&2
  exit 2
fi

COMMAND="$1"
shift
case "$COMMAND" in
  still) cmd_still "$@" ;;
  4k|video) cmd_4k "$@" ;;
  keypoint|kp) cmd_keypoint "$@" ;;
  extract) cmd_extract "$@" ;;
  bake) cmd_bake "$@" ;;
  asset|swasset) cmd_asset "$@" ;;
  --help|-h|help) usage ;;
  *)
    echo "unknown command: $COMMAND" >&2
    usage >&2
    exit 2
    ;;
esac
