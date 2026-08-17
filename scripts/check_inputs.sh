#!/usr/bin/env bash
# 校验 inputs/ 里的标定数据是否与清单一致（inputs/ 不在 git 里，见 docs/DATA.md）。
#
#   ./scripts/check_inputs.sh            # 校验两代
#   ./scripts/check_inputs.sh v1         # 只校验一代（只搬了一代时用）
#   ./scripts/check_inputs.sh v2         # 只校验二代
#   ./scripts/check_inputs.sh --write    # 用当前 inputs/ 重写 docs/data-manifest.tsv
#
# 数据不在 git，所以「我这份对不对」不再由 git 回答。三种坏法分别报出来：
#   MISSING    文件不在
#   TRUNCATED  大小不对（拷贝中断的典型症状）
#   CONTENT    大小对但内容不对（贴图版本错了——最危险，因为照样能跑，只是缝错位）
#   EXTRA      清单里没有的多余文件（不算失败）
#
# 只搬一代时另一代必然报缺失，那是正常的：用 v1 / v2 把范围缩到实际搬的那代。
#
# 清单里的路径由 python/stitch/profiles.py 与 python/fbx_overlay/profiles.py 导出，
# 不手写——加一条线后 --write 重新生成即可，本脚本不用改。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
fi

cd "$ROOT"
exec "$PY" -m python.dataset "$@"
