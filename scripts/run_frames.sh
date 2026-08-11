#!/usr/bin/env bash
# 快照整理/合成/标注/拼接统一入口（支持单数据集与多数据集融合、标米参数）。
#
# 子命令：
#   bash scripts/run_frames.sh organize [--date 20260807] [--cameras ...]
#       整理所有相机成帧文件夹；水下 16 相机额外做中值差分筛选
#       （detections.csv / curated.csv，对齐旧数据口径）。
#   bash scripts/run_frames.sh auto_merge --camera underA1 [--date 20260807]
#       自动合成指定相机（中值背景 + 差分前景叠加）。--camera 必填。
#         --dates D1 D2    跨数据集当一段合成（如横+竖两批拉线合成一张）。
#         --noise-gate 5   常态波动门控：偏离须 > 该像素 MAD × 倍率才算前景。
#                          水花/灯光反射每帧都在同一片区域晃（MAD 高）被滤掉，
#                          只在个别帧出现的目标（人工拉线、泳者）MAD 低被保留。
#                          0=关（默认，与老口径逐位一致）；3~8 为常用区间。
#         --pick peak      同一像素多帧命中时取偏离最大的一帧（拉线更清楚）；
#                          默认 last 后帧覆盖（泳者叠加要看运动轨迹）。
#   bash scripts/run_frames.sh merge [--date D] [--dates D1 D2 ...] [--cameras ...]
#       手动合成：mask 覆盖处取原帧、其余取中值背景。默认处理工程里所有相机。
#         --dates D1 D2       多数据集融合：同名相机帧按 --dates 顺序叠加
#                              （后叠在上层），帧号重编号；产物写第一个数据集。
#         --meter-overrides   帧→米数覆盖表，如 '28:14.5,34:17.0'；缺省按
#                              frame_meters 内置表（f1=0.5m 每帧+0.5，f28→f29
#                              补一帧、f34 重合不增）。
#       标注规则（自动，无需参数）：
#         underA*（水下 16 相机）标 `f<帧ID> <米数>`；其余相机（overhead /
#         gemini / femto / orbbec）完全不标。要水下也不标米数用 --meter-overrides
#         或按相机名判断不可改，只能手动删标签（不推荐）。
#   bash scripts/run_frames.sh grid [--date 20260807]
#       仅水下：16 相机 mask 合成图 4×4 cat 拼接，每格标注相机 ID + 米数。
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
#   bash scripts/run_frames.sh merge --dates A B C --cameras gemini_camera_1
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
    sed -n '2,35p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
  *)
    "$PY" -m python.labeling.frames "$@"
    ;;
esac
