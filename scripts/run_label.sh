#!/usr/bin/env bash
# 数据集标注工具（macOS / Linux）—— 都不涉及拼接，只处理标注数据本身。
#
#   ./scripts/run_label.sh 子命令 [选项…]
#
# 子命令：
#   mask              打开「保留区域」mask 标注器：选一台相机，逐帧拖拽画出该帧
#                     要保留的区域，存为数据集根目录下的 mask_label_project.json
#   dot               打开打点标注器：选 object-frames/ 目录，逐图打点
#   merge  [--cameras…]  把一台相机全时段快照合成一张 UV 参考图（另附中值背景帧）
#                     -> outputs/labeling/overhead-merge/
#   keypoint [选项…]  从 COCO-17 标注数据集生成按人裁剪的复核页
#                     -> outputs/keypoint_preview/index.html（双击即可看）
#
# 两个标注器都用 ES module，`file://` 下会被浏览器按 CORS 拦截（origin 为 null）
# 导致白屏，所以必须经这里走 http 打开，不要双击 html。加 --selftest 打开该标注器
# 的浏览器自测页。
#
# 数据集根用环境变量指定，不写死：
#   SWIM_UNDER_GRIDS_ROOT   水下快照 / 标注网格数据集（mask、dot、merge）
#
# 例：
#   ./scripts/run_label.sh mask
#   ./scripts/run_label.sh dot --port 9000
#   ./scripts/run_label.sh merge --cameras overhead5 overhead6
#   ./scripts/run_label.sh keypoint --dataset-root /path/to/2dkp
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

usage() {
  sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

(($# > 0)) || { usage; exit 2; }
COMMAND="$1"
shift
case "$COMMAND" in
  mask|dot)   exec "$PY" -m python.labeling.server "$COMMAND" "$@" ;;
  merge)      exec "$PY" -m python.labeling.merge_overhead "$@" ;;
  keypoint)
    "$PY" -m python.keypoints "$@"
    echo "在浏览器打开 outputs/keypoint_preview/index.html（或 --output-dir 指定的目录）"
    ;;
  --help|-h|help) usage; exit 0 ;;
  *) echo "未知子命令：$COMMAND" >&2; usage; exit 2 ;;
esac
