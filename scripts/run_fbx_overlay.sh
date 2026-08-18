#!/usr/bin/env bash
# 一键把入水机位 FBX 的网格画到相机原图上，并导出网格线的真实米数。
#
#   ./scripts/run_fbx_overlay.sh [输出目录] [--line water_entry|water_entry2 ...]
#
# 两条线（旧新并存，同 stitch 的 pool/pool2 模式，见 python/fbx_overlay/profiles.py）：
#   water_entry   旧 005/006 单 mesh（006.fbx/Plane004 垂直 + 005.fbx/Plane005 水面，
#                 底图 background.jpg）
#   water_entry2  femto + gemini 子相机（各含全屏矩形/垂直/水面）
#
# 产物落 outputs/<line>/overlay/<camera>/：
#   mesh.json                            完整几何 + 每个 vertex 的真实米数（供算法）
#   <camera>_mesh_overlay.png            合成图（所有非原图 mesh 叠加）
#   <camera>_<node>_<kind>_overlay.png   每个 mesh 单独一张
# 图上默认叠加米数标签，加 --no-labels 关闭（mesh.json 仍会写出）。
#
# 俯视机位（overhead / overhead2）不在这里：自上往下看，UV 不映射到任何单张
# 相机帧，它们是 stitch 的线。全景与米数都由那条链路一次出：
#   ./scripts/run_stitch.sh overhead2 extract,still
# extract 把米数写进 outputs/overhead2/mesh.json（一条线只有一份），
# still 出 stitch{,_grid,_spans,_heat,_label}.png，最后一张是泳道示意图叠加。
#
# 示例：
#   ./scripts/run_fbx_overlay.sh                              # 两条线
#   ./scripts/run_fbx_overlay.sh --line water_entry2
#   ./scripts/run_fbx_overlay.sh --camera gemini              # 兼容：映射到 water_entry2
#
# 注意：.fbm 纹理目录被 gitignore（**/*.fbm/），fresh clone 缺纹理时会报错，
# 需从原始资产恢复对应纹理。
#
# 旧单 mesh 显式回归仍可用 --mesh FBX NODE：
#   "$PY" -m python.fbx_overlay --mesh inputs/water_entry/models/006.fbx Plane004
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# The venv interpreter sits in bin/ on POSIX and Scripts/ on Windows. Check both
# before falling back: on Windows `python3` resolves to the WindowsApps App
# Execution Alias, which opens the Microsoft Store and exits 49 printing nothing
# — the script looks like it did no work at all.
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/.venv/Scripts/python.exe"
[[ -x "$PY" ]] || PY="$(command -v python3)"

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

cd "$ROOT"

# 位置参数是输出目录；不给就让每条线各自落到 outputs/<line>/overlay/。
# `${a[@]+...}` 而不是 `${a[@]}`：bash 3.2（macOS 自带）在 set -u 下把空数组
# 的展开当作未绑定变量报错。
OUT=()
if (($# > 0)) && [[ "${1:0:1}" != - ]]; then
  OUT=("--output" "$1")
  shift
fi

"$PY" -m python.fbx_overlay ${OUT[@]+"${OUT[@]}"} "$@"
