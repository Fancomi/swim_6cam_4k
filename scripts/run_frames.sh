#!/usr/bin/env bash
# 快照整理/合成/标注/拼接统一入口。
#
#   bash scripts/run_frames.sh organize [--date 20260807] [--cameras ...]
#       整理所有相机成帧文件夹；水下 16 相机额外做中值差分筛选
#       （detections.csv / curated.csv，对齐旧数据口径）。
#   bash scripts/run_frames.sh auto_merge --camera underA1 [--date 20260807]
#       自动合成指定相机（中值背景 + 差分前景叠加）。--camera 必填。
#   bash scripts/run_frames.sh merge [--project /path/mask_label_project.json]
#       手动合成：mask 覆盖处取原帧、其余取中值背景，处理工程里所有相机。
#   bash scripts/run_frames.sh grid [--project ...]
#       仅水下：16 相机 mask 合成图 4×4 cat 拼接，每格标注相机 ID + 米数，
#       工程给定时再标每帧 mask 的帧 ID + 米数。
#   bash scripts/run_frames.sh label [--port 8765]
#       打开浏览器 mask 标注器（选目录即通用：overhead/underwater/femto/gemini）。
#
# 数据根用 SWIM_UNDER_GRIDS_ROOT 覆盖（默认 swimming-xlj-under-grids），
# 产物统一到 <数据集根>/<日期>/object-frames/。
#
# 用法:
#   bash scripts/run_frames.sh organize
#   bash scripts/run_frames.sh auto_merge --camera underA1
#   bash scripts/run_frames.sh merge
#   bash scripts/run_frames.sh grid
#   bash scripts/run_frames.sh label
#   bash scripts/run_frames.sh help
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

case "${1:-}" in
  help|-h|--help)
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
  *)
    "$PY" -m python.labeling.frames "$@"
    ;;
esac
