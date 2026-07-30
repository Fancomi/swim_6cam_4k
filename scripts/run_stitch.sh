#!/usr/bin/env bash
# 相机拼接统一入口（macOS / Linux）—— 三条线共用这一个脚本。
#
#   ./scripts/run_stitch.sh LINE STEPS [选项…]
#
# LINE  哪条相机线：
#   pool        六路 4K 俯视泳池（两排机位，距离羽化，5001x2101）
#   underwater  水下 16 块平面，一条泳道从下往上看（竖直硬缝，6001x656）
#   overhead    水上 2 块平面，同一条泳道从上往下看（4251x511）
#
# STEPS 逗号分隔、按给出顺序执行：
#   extract  FBX          -> outputs/<line>/mesh.json
#   tex      相机首帧      -> outputs/<line>/ref_tex/<camera>.png
#   still    静图 + 网格诊断图 + 融合热图
#   video    每路片段      -> outputs/<line>/stitch.mp4
#   asset    mesh.json    -> build/assets/generated/<line>.swasset
#   build    构建 swim_realtime
#   live     实时拼接（预览窗口 / HEVC / 指标）
#
# 例：
#   ./scripts/run_stitch.sh pool extract,still            # 泳池静图
#   ./scripts/run_stitch.sh pool video --seconds-float 10 # 泳池 10 秒拼接视频
#   ./scripts/run_stitch.sh overhead extract,still
#   ./scripts/run_stitch.sh overhead extract,asset,build,live --video-dir DIR
#   ./scripts/run_stitch.sh underwater still --real       # 用相机首帧而非标定图
#
# 全部逻辑在 python/stitch/（mac 与 Windows 共用同一份），这个脚本只挑解释器并
# 原样转发参数。Windows 用 scripts/run_stitch.ps1，双击用 scripts\run_win.bat。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

if (($# < 2)); then
  # 用法说明只有一份：把本文件顶部的注释块打出来，不再复制一遍。
  sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  echo "完整选项：python -m python.stitch --help" >&2
  exit 2
fi

cd "$ROOT"
exec "$PY" -m python.stitch "$@"
