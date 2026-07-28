#!/usr/bin/env bash
# 入水检测机位（water entry）难例筛选全流程：预测 -> 选帧 -> 复核页 -> 交付包。
#
# 新增片段后重跑这一个脚本即可全量刷新：predict 会把新片段一并纳入（manifest.csv
# 是唯一的片段清单来源），后续每一步都从 predict 的产物重算，没有增量状态。
#
# 筛选阈值的默认值来自 python/water_entry/select_frames.py 的 DEFAULT_* 常量，
# 本脚本不复制它们——要改口径就改那里，两个入口同时生效。
#
# 用法:
#   ./scripts/run_water_entry.sh                    # 全流程，默认参数
#   ./scripts/run_water_entry.sh --skip-predict     # 复用已有预测，只重跑筛选与出包
#   ./scripts/run_water_entry.sh --kp 0.10          # 收紧关键点分歧阈值（选出更少）
#   ./scripts/run_water_entry.sh --preview 0        # 质检页渲染全部候选（默认前 120）
#   ./scripts/run_water_entry.sh --no-package       # 不出交付包
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
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

usage() {
  sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while (($#)); do
  case "$1" in
    --skip-predict) SKIP_PREDICT=1; shift ;;
    --kp) KP_MEAN_NORM="$2"; shift 2 ;;
    --min-gap) MIN_GAP="$2"; shift 2 ;;
    --max-offset) MAX_OFFSET="$2"; shift 2 ;;
    --preview) PREVIEW_LIMIT="$2"; shift 2 ;;
    --no-package) DO_PACKAGE=0; shift ;;
    --help|-h) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

step() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

step "1/4 预测（swimup / swimup_bk / coco 三模型 × 全部片段）"
if ((SKIP_PREDICT)); then
  echo "跳过（--skip-predict），复用 outputs/water_entry/predict/"
else
  "$PY" -m python.water_entry.predict
fi

step "2/4 难例筛选（kp=${KP_MEAN_NORM} min-gap=${MIN_GAP} max-offset=${MAX_OFFSET}）"
"$PY" -m python.water_entry.select_frames \
  --kp-mean-norm "$KP_MEAN_NORM" \
  --min-gap "$MIN_GAP" \
  --max-offset "$MAX_OFFSET"

step "3/4 质检页（前 ${PREVIEW_LIMIT} 帧）"
"$PY" -m python.water_entry.annotate_preview --limit "$PREVIEW_LIMIT"

if ((DO_PACKAGE)); then
  step "4/4 交付包（原始帧 + manifest + COCO 预标注 + 说明）"
  "$PY" -m python.water_entry.export_package
else
  step "4/4 交付包：跳过（--no-package）"
fi

cat <<EOF

产物：
  outputs/water_entry/predict/<model>/metrics.csv     逐片段模型指标
  outputs/water_entry/annotate_candidates.csv         难例候选清单
  outputs/water_entry/annotate_preview/index.html     候选帧质检页（浏览器打开）
  outputs/water_entry/annotate_package.zip            交付给标注的数据包
EOF