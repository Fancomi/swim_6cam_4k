#!/usr/bin/env bash
# 一键跑「相机微动自动对齐」的 标定版本 × 数据日期 交叉矩阵。
#
#   ./scripts/run_align.sh [--only 单元或线名 ...] [--video 秒数] [--model 变换] [--dry-run]
#
# 修正只有和它的替代方案比才知道值不值，所以跑的是一个**交叉**：每个机位有两版标定、
# 两个时期的数据，每种组合一格。每格用同一批像素渲两遍（off = 原标定，on = UV 被修正
# 过），差别只有 UV。
#
#               数据时期1                数据时期2
#   标定 v1     同期：地板线             交叉：v1 漂到时期2 的数据上
#   标定 v2     交叉：v2 拉回时期1       同期：地板线
#
# 对角线是参照——标定配自己那期数据就是「没有微动」的样子，对齐器应该几乎不动它；
# 非对角线才是实验，而且**两个方向都跑**：只在时间正向上有效的修正，说明拟合的是数据集
# 而不是漂移。入水机位那两格还能对真值打分，因为两版标定是同一份几何（顶点逐位相同）
# 在各自那期的图上手工重标的，互为对方的真值。
#
#   underwater   × 202607   标定 v1（all.fbx）配自己那期      —— 地板线
#   underwater   × 202608   同一份 v1 六周后                  —— 交叉
#   underwater2  × 202607   标定 v2（8.15.fbx）拉回旧那期      —— 交叉（反向）
#   underwater2  × 202608   v2 配自己那期                     —— 地板线
#   water_entry  × 0708     v1 配自己那帧                     —— 地板线
#   water_entry  × 0807     v1 打到新帧                       —— 交叉，有真值
#   water_entry2 × 0708     v2 打到旧帧                       —— 交叉（反向），有真值
#   water_entry2 × 0807     v2 配自己那帧                     —— 地板线
#   gemini       × 0807-H   只有一版标定，做负对照            —— 见下
#
# **gemini 凑不出交叉，也没装作能凑**：它只有一版标定（20260708 目录里那张
# gemini_camera_1_merged.png 与 0807 那张像素相关 0.9994，是同一份的副本，不是更早的
# 一次标定）。所以那一格改作负对照：拿同一天另一场（相机确实没动）的图去配准，对齐器
# 必须报 ~0、也不该有增益。实测 0.01~0.12px。一个不会「保持不动」的漂移修正器比没有更糟。
#
# 单元是数据（python/align/__main__.py 的 CELLS），加一格就是加一条记录。
#
# 产物：
#   outputs/<line>/align/<key>/stitch_{off,on}{,_grid,_spans,_heat}.png   拼接线静图
#   outputs/<line>/align/<key>/video_{off,on}.mp4                        拼接线视频（--video）
#   outputs/<line>/align/<key>/overlay_{off,on}.png                      入水机位
#   outputs/<line>/align/<key>/{align.json,cameras.csv}                  每相机矩阵与是否采纳
#   outputs/align/summary.csv + index.html                               矩阵汇总
#
# `--video 秒数`（裸给 --video 即 4 秒）另出 off/on 两条全景视频。默认不出：每版约 90s，
# 而静图一对只要约 15s。静图是中值背景，能看结构对不对齐；视频能看**观众实际看到的**——
# 泳者穿过错缝时的跳动、拉伸、瞬间重影，那是没有泳者的一帧里看不见的。
#
# 估计器是相位相关播种的金字塔 ECC（不是 SIFT：泳池瓷砖周期性纹理会让特征匹配
# 在错一格的位置上「自信地」锁死，underA1/A9 实测偏 −116px 而真值约 0）。
# 每台相机独立判定，不合理或没有增益就整台回退原标定。
#
# 解一整条线约 10s，结果按 (标定纹理内容, 探针图内容) 落缓存，同一份 (标定, 日期)
# 只算一次；--force 强制重算，--no-cache 不落缓存。
# `--only` 只跑部分格子时，summary 只含跑过的那几格并在页面顶部标注「Partial run」——
# 混进上一次的行会让不同代码/数据的结果并排却看不出来。
#
# 示例：
#   ./scripts/run_align.sh                      # 全部单元（不出视频）
#   ./scripts/run_align.sh --video 4            # 加 4 秒 off/on 视频
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
  # 用法说明只有一份：把顶部注释块（第 2 行到 set -euo 前一行）打出来。行号写死过一次
  # 就漏掉了末尾两行，所以这里按标记算，改注释不必再回来数行。
  END=$(($(grep -n '^set -euo' "${BASH_SOURCE[0]}" | cut -d: -f1) - 1))
  sed -n "2,${END}p" "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

cd "$ROOT"
"$PY" -m python.align "$@"
