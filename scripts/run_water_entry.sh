#!/usr/bin/env bash
# 入水检测机位（water entry）难例筛选全流程：预测 -> 选帧 -> 质检页 -> 交付包。
#
# 与三条拼接线无关的第四类相机：水下 0 号平面正上方的单个 Orbbec 机位，
# 用于仰泳蹬壁出发的空中反弓与入水姿态识别。代码在 python/water_entry/。
#
# 新增片段后重跑这一个脚本即可全量刷新：predict 会把新片段一并纳入（manifest.csv
# 是唯一的片段清单来源），后续每一步都从 predict 的产物重算，没有增量状态。
#
# 筛选阈值的默认值来自 python/water_entry/select_frames.py 的 DEFAULT_* 常量，
# 本脚本不复制它们——要改口径就改那里，两个入口同时生效。
#
# 产物分开两处：
#   outputs/pose/ = 姿态检测链（predict / annotate_* / review / weights）
#   outputs/water_entry/calib/ = 标定任务（对齐缓存 align/、叠图 overlay/）
# 数据集根用 WATER_ENTRY_DATASET_ROOT 覆盖；预测根仍落在 outputs/pose/。
# 预测默认对比四个模型：swimup（现网）、swimup_bk（随包微调版）、
# yolo26（基于难例数据再训练的 yolo26m-pose，位于数据集根兄弟目录）、coco（通用）。
#
# 用法:
#   ./scripts/run_water_entry.sh                    # 全流程，默认参数
#   ./scripts/run_water_entry.sh --skip-predict     # 复用已有预测，只重跑筛选与出包
#   ./scripts/run_water_entry.sh --kp 0.10          # 收紧关键点分歧阈值（选出更少）
#   ./scripts/run_water_entry.sh --preview 0        # 质检页渲染全部候选（默认前 120）
#   ./scripts/run_water_entry.sh --no-package       # 不出交付包
#   ./scripts/run_water_entry.sh --no-overlay       # 不画水面/纵向标定叠图（最后一步）
#   单步复核页：python -m python.water_entry.review --clips 20260725-160224
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

# 筛选口径不在这里写死：select_frames 的 DEFAULT_* 是唯一来源，脚本读取它，
# 这样「走流程脚本」与「直接调 we-select」永远给出同一批候选帧。
read -r KP_MEAN_NORM MIN_GAP MAX_OFFSET <<<"$(
  "$PY" -c 'from python.water_entry import select_frames as S
print(S.DEFAULT_KP_MEAN_NORM, S.DEFAULT_MIN_GAP, S.DEFAULT_MAX_OFFSET)'
)"
PREVIEW_LIMIT=120       # 质检页渲染帧数：0 = 全部（仅影响人工翻页，不影响候选集）
SKIP_PREDICT=0
DO_PACKAGE=1
DO_OVERLAY=1            # 最后一步：水面/纵向标定叠图（默认画；--no-overlay 关闭）

usage() {
  sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while (($#)); do
  case "$1" in
    --skip-predict) SKIP_PREDICT=1; shift ;;
    --kp) KP_MEAN_NORM="$2"; shift 2 ;;
    --min-gap) MIN_GAP="$2"; shift 2 ;;
    --max-offset) MAX_OFFSET="$2"; shift 2 ;;
    --preview) PREVIEW_LIMIT="$2"; shift 2 ;;
    --no-package) DO_PACKAGE=0; shift ;;
    --no-overlay) DO_OVERLAY=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

step "1/5 预测（swimup / swimup_bk / yolo26 / coco 四模型 × 全部片段）"
if ((SKIP_PREDICT)); then
  echo "跳过（--skip-predict），复用 outputs/pose/predict/"
else
  "$PY" -m python.water_entry.predict
fi

step "2/5 难例筛选（kp=${KP_MEAN_NORM} min-gap=${MIN_GAP} max-offset=${MAX_OFFSET}）"
"$PY" -m python.water_entry.select_frames \
  --kp-mean-norm "$KP_MEAN_NORM" \
  --min-gap "$MIN_GAP" \
  --max-offset "$MAX_OFFSET"

step "3/5 质检页（前 ${PREVIEW_LIMIT} 帧）"
"$PY" -m python.water_entry.annotate_preview --limit "$PREVIEW_LIMIT"

if ((DO_PACKAGE)); then
  step "4/5 交付包（原始帧 + manifest + COCO 预标注 + 说明）"
  "$PY" -m python.water_entry.export_package
else
  step "4/5 交付包：跳过（--no-package）"
fi

if ((DO_OVERLAY)); then
  step "5/5 水面/纵向标定叠图（透明，作用于相机原图）"
  "$PY" -m python.water_entry.overlay
  "$PY" -m python.water_entry.overlay --line water_entry2
else
  step "5/5 标定叠图：跳过（--no-overlay）"
fi

cat <<EOF

产物：
  outputs/pose/predict/<model>/metrics.csv     逐片段模型指标
  outputs/pose/annotate_candidates.csv         难例候选清单
  outputs/pose/annotate_preview/index.html     候选帧质检页（浏览器打开）
  outputs/pose/annotate_package.zip            交付给标注的数据包
  outputs/water_entry/calib/overlay/<line>/overlay.png   水面/纵向标定透明叠图
  outputs/water_entry/calib/overlay/<line>/overlay.composite.png 叠图合成到相机原图
EOF