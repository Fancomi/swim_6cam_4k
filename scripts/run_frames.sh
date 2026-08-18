#!/usr/bin/env bash
# 快照整理/合成/标注/拼接统一入口（支持单数据集与多数据集融合、标米口径外置）。
#
# 一键出全部标定产物（配方在 python/labeling/frames.py 的 PRODUCTS，可复现）：
#   bash scripts/run_frames.sh products                 # 四类全跑
#   bash scripts/run_frames.sh products --only sixcam   # 只跑一类
#   bash scripts/run_frames.sh products --dry-run       # 只打印要跑什么
#     四类：underwater 水下16相机 mask（带帧号+米数）+4×4拼接
#           sixcam     横竖合并的 6 相机拉线自动合成（MAD 门控），落 Horizontal
#           overhead   20260708+Horizontal 融合的 overhead5/6 mask，落 20260708
#           entry      gemini/femto 各数据集单独 mask（20260708 是 orbbec_camera_1）
#
# 单步子命令：
#   bash scripts/run_frames.sh organize [--date 20260807] [--cameras ...]
#       整理所有相机成帧文件夹；水下 16 相机额外做中值差分筛选
#       （detections.csv / curated.csv，对齐旧数据口径）。
#   bash scripts/run_frames.sh auto_merge --camera underA1 [--date 20260807]
#       自动合成指定相机（中值背景 + 差分前景叠加）。--camera 必填。
#         --dates D1 D2    跨数据集当一段合成（如横+竖两批拉线合成一张）。
#       前景 = 与中值背景的 RGB 距离 > --thresh，按时间序后帧覆盖前帧。
#       带高按内存预算自动收窄（帧数多时无需手动调 --band-rows）。
#   bash scripts/run_frames.sh merge [--date D] [--dates D1 D2 ...] [--cameras ...]
#       手动合成：mask 覆盖处取原帧、其余取中值背景。默认处理工程里所有相机。
#         --dates D1 D2    多数据集融合：同名相机帧按 --dates 顺序叠加（后叠在
#                          上层）并统一重编号；只给一个日期时保留工程里的帧号。
#         --meter-spec     帧→米数口径 json（默认 <snapshots>/frame_meters.json）：
#                          {"schema":"frame-meters/v1","start":0.5,"step":0.5,
#                           "gaps":[28],"skip":[35]}
#                          gaps=该帧后缺一帧（米数跳一格），skip=该帧是重复帧
#                          （不标不占位）。文件不存在按等距 0.5m 递增。
#         --meter-overrides 临时纠个别帧，如 '28:14.5'；长期口径写进 --meter-spec。
#         --merged-suffix   合成图后缀（默认水下 mask_merged 给 grid 读、其余 merged）。
#       标注规则（按相机自动判断）：underA* 标 `f<帧ID> <米数>`；其余相机
#       （overhead / gemini / femto / orbbec）完全不标。
#   bash scripts/run_frames.sh grid [--date 20260807]
#       仅水下：16 相机 mask 合成图 4×4 cat 拼接，每格标注相机 ID + 米数。
#   bash scripts/run_frames.sh label [--port 8765]
#       打开浏览器 mask 标注器（选目录即通用：overhead/underwater/femto/gemini）。
#
# 数据根用 SWIM_UNDER_GRIDS_ROOT 覆盖（默认 swimming-xlj-under-grids），
# 产物统一到 <数据集根>/<日期>/object-frames/。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
# The venv interpreter sits in bin/ on POSIX and Scripts/ on Windows. Check both
# before falling back: on Windows `python3` resolves to the WindowsApps App
# Execution Alias, which opens the Microsoft Store and exits 49 printing nothing
# — the script looks like it did no work at all.
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$ROOT/.venv/Scripts/python.exe"
[[ -x "$PY" ]] || PY="$(command -v python3)"

case "${1:-}" in
  help|-h|--help)
    sed -n '2,47p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    ;;
  *)
    "$PY" -m python.labeling.frames "$@"
    ;;
esac
