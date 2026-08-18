#!/usr/bin/env bash
# 渲染某条对齐线路的 off/on 全景**完整视频**，默认遍历 202607 目录的全部样本。
#
# 对齐矩阵的 --video 默认只出 ~4 秒（一版约 90s）；「完整窗口」用于看泳者穿过错缝的
# 那一瞬——跳动/拉伸/重影在短窗口里可能落不进去。本脚本从已算好的 align.json 直接
# 渲染（不重解，只重渲），off/on 同一批片段、同一套时间对齐，逐帧可比。
# 一天的漂移修正只解一次，整批样本共用同一套 UV，所以默认按「数据日期」整目录遍历。
#
# 用法:
#   ./scripts/run_align_videos.sh                       # 202607 全部样本 × underwater2
#   ./scripts/run_align_videos.sh --line underwater --data 202608
#   ./scripts/run_align_videos.sh --sample swb_..._12  # 只跑一个样本
#   ./scripts/run_align_videos.sh --seconds 6          # 每个窗口截到 6 秒
#   ./scripts/run_align_videos.sh --help
#
# 前提：先跑过 ./scripts/run_align.sh（或 run_align.sh --only <格>），否则没有
# align.json 缓存；缺缓存的样本会被跳过并列出。产物：
#   outputs/<line>/align/<data>_<sample>/full_video_{off,on}.mp4（已存在则跳过）。
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
  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

cd "$ROOT"
"$PY" -m python.align.videos "$@"
