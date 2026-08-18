#!/usr/bin/env bash
# 一键跑「相机微动自动对齐」的标定版本 × 数据日期 交叉矩阵。
#
#   ./scripts/run_align.sh [--only 单元或线名 ...] [--model 变换] [--dry-run]
#
# 为什么要交叉矩阵：标定绑定的是「制作标定时那一张图」的 UV，相机一被碰就整体偏。
# 单跑一次没法判断修正值不值——每个单元固定一份标定和一份数据，用同一批像素渲两遍
# （off = 原标定，on = UV 被修正过），差别只有 UV。
#
#   underwater   × 202607   老标定 + 同期数据：无微动时的地板线
#   underwater   × 202608   同一份标定六周后：微动最大，也最像现场
#   underwater2  × 202608   重标的标定 + 同期数据：手工重标能到什么水平
#   water_entry  × 0807     老 femto UV 打到新帧上——而 water_entry2/femto 正是
#                           同一份几何在这一帧上手工重标的结果，所以这一格有真值
#   water_entry2 × 0807     那份真值本身，作为对照
#
# 单元是数据（python/align/__main__.py 的 CELLS），加一格就是加一条记录。
#
# 产物：
#   outputs/<line>/align/<key>/stitch_{off,on}{,_grid,_spans,_heat}.png   拼接线
#   outputs/<line>/align/<key>/overlay_{off,on}.png                      入水机位
#   outputs/<line>/align/<key>/{align.json,cameras.csv}                  每相机矩阵与是否采纳
#   outputs/align/summary.csv + index.html                               矩阵汇总
#
# 估计器是相位相关播种的金字塔 ECC（不是 SIFT：泳池瓷砖周期性纹理会让特征匹配
# 在错一格的位置上「自信地」锁死，underA1/A9 实测偏 −116px 而真值约 0）。
# 每台相机独立判定，不合理或没有增益就整台回退原标定。
#
# 解一整条线约 10s，结果按 (标定纹理内容, 探针图内容) 落缓存，同一份 (标定, 日期)
# 只算一次；--force 强制重算，--no-cache 不落缓存。
#
# 示例：
#   ./scripts/run_align.sh                      # 全部单元
#   ./scripts/run_align.sh --only underwater    # 只跑这条线的格子
#   ./scripts/run_align.sh --dry-run            # 只打印计划
#
# 单点复核（不出矩阵，直接看某条线开/关对齐的 _grid）：
#   ./scripts/run_stitch.sh underwater still --align-from <片段目录>
#   ./scripts/run_fbx_overlay.sh --line water_entry2 --align-to femto=<新图>
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
  sed -n '2,42p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

cd "$ROOT"
"$PY" -m python.align "$@"
