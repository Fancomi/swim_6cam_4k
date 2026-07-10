#!/usr/bin/env bash
# 复现 4K 泳池拼接合成。
#
# 沿用 swim_fbx_demo 的对齐（pool_mesh.json，烘焙 UV），对新一批 4K 同步视频做拼接。
# 本脚本调用 src/render_pool.py，并固化可复现的参数。
#
# 素材： ${SWIMMING_DATASET_DIR:-/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K}
#   6 路 20260629_172532_camN.mp4，3840x2160 @ 29.97fps，约 602s。
# mesh 顺序为 camera 3/2/1/4/5/6，故视频按 cam3 cam2 cam1 cam4 cam5 cam6 传入。
# 输出画布 5002x2102（老 1080p 版本的 2 倍分辨率）。
#
# 用法:
#   ./scripts/run_4k.sh                 # 默认渲 10s 测试片
#   ./scripts/run_4k.sh 30              # 渲 30s
#   ./scripts/run_4k.sh 602 outputs/videos/pool_4k_full.mp4   # 渲全长到指定文件
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SECONDS_ARG="${1:-10}"
DATASET_DIR="${SWIMMING_DATASET_DIR:-/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K}"
SESSION="20260629_172532"
OUT="${2:-$PROJECT_ROOT/outputs/videos/pool_4k_test${SECONDS_ARG}s.mp4}"
PY="$PROJECT_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"

VIDEOS=(
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

for video in "${VIDEOS[@]}"; do
  [[ -f "$video" ]] || {
    echo "video not found: $video" >&2
    exit 1
  }
done

mkdir -p "$(dirname "$OUT")"

"$PY" "$PROJECT_ROOT/src/render_pool.py" \
  --data "$PROJECT_ROOT/outputs/data/pool_mesh.json" \
  --videos "${VIDEOS[@]}" \
  --video "$OUT" \
  --seconds "$SECONDS_ARG"

echo "done -> $OUT"
