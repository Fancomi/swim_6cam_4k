#!/usr/bin/env bash
# Python 离线应用统一入口：静图/4K 拼接、关键点预览、FBX 提取与运行时资产。
#
# 用法:
#   ./scripts/run_python.sh still
#   ./scripts/run_python.sh 4k [SECONDS] [OUTPUT_MP4]
#   ./scripts/run_python.sh keypoint [--dataset-root PATH] [...]
#   ./scripts/run_python.sh oh-merge [--cameras ...] [--thresh N]
#   ./scripts/run_python.sh label mask|dot [--port N] [--selftest]
#   ./scripts/run_python.sh extract [SRC_FBX] [DST_JSON]
#   ./scripts/run_python.sh bake SRC_FBX DST_FBX [--ext-px N]
#   ./scripts/run_python.sh asset [INPUT_JSON] [OUTPUT_SWASSET]
#   ./scripts/run_python.sh we-predict [...]     # water-entry cam: YOLO-pose predict
#   ./scripts/run_python.sh we-review [...]      # water-entry cam: HTML review page
#   ./scripts/run_python.sh we-select [...]      # water-entry cam: pick frames to annotate
#   ./scripts/run_python.sh we-annotate [...]    # water-entry cam: candidate QC page
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

DATASET_DIR="${SWIMMING_DATASET_DIR:-/Users/penghaotian/Downloads/DATAS/SWIMMING/swim-6cam-4k/20260629-4K-raw}"
SESSION="20260629_172532"

usage() {
  cat <<'EOF'
Usage: scripts/run_python.sh <command> [args]

Commands:
  still      Render static stitch PNG from composite textures
  4k         Render 4K six-camera H.264 stitch (default 10s test clip)
  keypoint   Build COCO-17 person-crop HTML review page
  oh-merge   Merge each overhead/orbbec camera's snapshots into one UV reference
  label      Serve a browser labeler over http: mask (keep-region) or dot (points)
  extract    Extract pool FBX mesh JSON
  bake       Bake centre-line UV extension into a new FBX
  asset      Compile mesh JSON into GPU runtime .swasset
  we-predict Run YOLO-pose over the water-entry camera clips (multi-model compare)
  we-review  Build the water-entry pose review HTML page from predict results
  we-select  Pick badly-predicted frames as incremental annotation candidates
  we-annotate Render the candidate frames into a QC page before annotation
  (拼接线路 underwater/overhead 已移到 scripts/run_stitch.sh)

Examples:
  ./scripts/run_python.sh still
  ./scripts/run_python.sh 4k
  ./scripts/run_python.sh 4k 30
  ./scripts/run_python.sh 4k 602 outputs/videos/pool_4k_full.mp4
  ./scripts/run_python.sh keypoint
  ./scripts/run_python.sh extract
  ./scripts/run_python.sh asset
  ./scripts/run_python.sh we-predict --limit 5
  ./scripts/run_python.sh we-review --clips 20260725-160224
  ./scripts/run_python.sh we-select
  ./scripts/run_python.sh we-annotate --limit 100
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

cmd_oh_merge() {
  "$PY" -m python.annotation_preview.merge_overhead "$@"
}

cmd_label() {
  "$PY" -m python.annotation_preview.labeler_server "$@"
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
  local output_swasset="${2:-$ROOT/build/assets/generated/pool_4k.swasset}"
  shift $(( $# >= 2 ? 2 : $# )) || true
  mkdir -p "$(dirname "$output_swasset")"
  "$PY" -m python.assets.compile_runtime_asset \
    "$input_json" "$output_swasset" \
    --camera-ids cam3 cam2 cam1 cam4 cam5 cam6 --ppm 100 \
    "$@"
  echo "done -> $output_swasset"
}

# --- water-entry camera (underwater plane 0 上方机位) -------------------------
# 入水检测机位的 YOLO-pose 预测、复核与增量标注选帧；
# 数据集根用 WATER_ENTRY_DATASET_ROOT 覆盖。
cmd_we_predict() {
  "$PY" -m python.water_entry.predict "$@"
}

cmd_we_review() {
  "$PY" -m python.water_entry.review "$@"
  echo "open outputs/water_entry/review/index.html in a browser"
}

cmd_we_select() {
  "$PY" -m python.water_entry.select_frames "$@"
}

cmd_we_annotate() {
  "$PY" -m python.water_entry.annotate_preview "$@"
  echo "open outputs/water_entry/annotate_preview/index.html in a browser"
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
  oh-merge) cmd_oh_merge "$@" ;;
  label) cmd_label "$@" ;;
  extract) cmd_extract "$@" ;;
  bake) cmd_bake "$@" ;;
  asset|swasset) cmd_asset "$@" ;;
  we-predict) cmd_we_predict "$@" ;;
  we-review) cmd_we_review "$@" ;;
  we-select) cmd_we_select "$@" ;;
  we-annotate) cmd_we_annotate "$@" ;;
  --help|-h|help) usage ;;
  *)
    echo "unknown command: $COMMAND" >&2
    usage >&2
    exit 2
    ;;
esac
