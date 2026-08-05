#!/usr/bin/env bash
# 一键生成 water-entry FBX 网格检查图。
#
#   ./scripts/run_fbx_overlay.sh [输出目录]
#
# 固定输入：
#   底图       inputs/water_entry/background.jpg
#   Plane004   inputs/water_entry/models/006.fbx
#   Plane005   inputs/water_entry/models/005.fbx
#
# 默认输出到 outputs/water_entry/：
#   fbx_mesh_overlay.png       两个网格叠加
#   plane004_mesh_overlay.png  仅 Plane004
#   plane005_mesh_overlay.png  仅 Plane005
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

if (($# > 1)) || [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit $(( $# > 1 ? 2 : 0 ))
fi

cd "$ROOT"
OUT_DIR="${1:-outputs/water_entry}"
mkdir -p "$OUT_DIR"

"$PY" -m python.fbx_overlay \
  --output "$OUT_DIR/fbx_mesh_overlay.png"

"$PY" -m python.fbx_overlay \
  --output "$OUT_DIR/plane004_mesh_overlay.png" \
  --mesh inputs/water_entry/models/006.fbx Plane004

"$PY" -m python.fbx_overlay \
  --output "$OUT_DIR/plane005_mesh_overlay.png" \
  --mesh inputs/water_entry/models/005.fbx Plane005

printf 'wrote three mesh overlays to %s\n' "$OUT_DIR"
