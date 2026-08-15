#!/usr/bin/env bash
# 一键生成 FBX 网格检查图（每相机一合成图 + 每 mesh 一图）。
#
#   ./scripts/run_fbx_overlay.sh [输出目录] [--line water_entry|water_entry2|overhead|overhead2 ...]
#
# 四条线（旧新并存，同 stitch 的 pool/pool2 模式，见 python/fbx_overlay/profiles.py）：
#   water_entry   旧 005/006 单 mesh（006.fbx/Plane004 垂直 + 005.fbx/Plane005 水面，
#                底图 background.jpg）
#   water_entry2  femto + gemini 子相机（各含全屏矩形/垂直/水面）
#   overhead      旧 002.fbx 俯视两平面（镜像 stitch overhead 线）
#   overhead2     新 25 水面.fbx 俯视两平面（重建网格）
#
# base_image 线（water_entry*）从 FBX 全屏矩形纹理或 background.jpg 取底图；
# canvas 线（overhead*）用 stitch Canvas 拼接成全景（4255x515），
# 可 --texture-set fbx|dataset 切换纹理组，并默认输出与 label_line.png 的对比图。
#
# 默认输出（base_image 线到 outputs/<line>/<camera>/）：
#   <camera>_mesh_overlay.png              合成图（所有非原图 mesh 叠加）
#   <camera>_<node>_<kind>_overlay.png     每个 mesh 单独一张
#   mesh.json                              完整几何 + 每 vertex 真实米数（供算法）
#
# 示例：
#   ./scripts/run_fbx_overlay.sh                              # water_entry + water_entry2
#   ./scripts/run_fbx_overlay.sh --line water_entry2
#   ./scripts/run_fbx_overlay.sh --camera gemini              # 兼容：映射到 water_entry2
#   ./scripts/run_fbx_overlay.sh outputs/overhead2 --line overhead2
#   ./scripts/run_fbx_overlay.sh outputs/overhead2 --line overhead2 --texture-set dataset
#
# 合成图/单 mesh 图默认叠加米数标签，加 --no-labels 关闭（mesh.json 仍会写出）。
#
# 注意：.fbm 纹理目录被 gitignore（**/*.fbm/），fresh clone 缺纹理时会报错，
# 需从原始资产恢复对应纹理。旧 outputs/water_entry/femto|gemini 已随 line 重构
# 迁移到 outputs/water_entry2/ 下，陈旧产物属历史遗留。
#
# 旧单 mesh 显式回归仍可用 --mesh FBX NODE：
#   "$PY" -m python.fbx_overlay --mesh inputs/water_entry/models/006.fbx Plane004
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

cd "$ROOT"

if (($# > 0)) && [[ "${1:0:1}" != - ]]; then
  OUT_DIR="$1"
  shift
else
  OUT_DIR="outputs/water_entry"
fi

mkdir -p "$OUT_DIR"

"$PY" -m python.fbx_overlay --output "$OUT_DIR" "$@"

printf 'wrote camera overlays to %s\n' "$OUT_DIR"
