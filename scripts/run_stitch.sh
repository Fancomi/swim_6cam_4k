#!/usr/bin/env bash
# 平面拼接统一入口（macOS / Linux）。
#
# 用法:
#   ./scripts/run_stitch.sh PROFILE STEPS [选项…]
#
# 例:
#   ./scripts/run_stitch.sh overhead extract,still
#   ./scripts/run_stitch.sh underwater still --real --blend-px 120
#   ./scripts/run_stitch.sh overhead extract,asset,build,live --video-dir DIR
#
# 全部逻辑在 python/stitch/__main__.py（mac 与 Windows 共用同一份），这个脚本只
# 负责挑选解释器并原样转发。Windows 用 scripts/run_stitch.ps1。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

if (($# < 2)); then
  cat >&2 <<'EOF'
Usage: scripts/run_stitch.sh PROFILE STEPS [options]

PROFILE  underwater | overhead
STEPS    逗号分隔，按给出顺序执行:
           extract  FBX -> mesh JSON
           tex      导出每台相机的参考贴图（首帧）
           still    静图 + 网格诊断图 + 融合热图
           video    每路片段 -> 全景 mp4
           asset    mesh JSON -> GPU .swasset
           build    构建 swim_realtime
           live     实时拼接（预览 / HEVC / 指标）

Common options (full list: --help):
  --video-dir DIR    片段目录（video / live，以及从视频取贴图的 tex 必需）
  --real             still 用导出的相机帧，而非设计师标定图
  --seconds N        live 运行秒数（默认 30）
  --encode           live 同时写出 HEVC
  --no-window        live 离屏渲染
  --blend-px N       覆盖 profile 的接缝过渡宽度
  --ppm N            覆盖 profile 的每米像素数
  --force            即使产物是新的也重做
EOF
  exit 2
fi

cd "$ROOT"
exec "$PY" -m python.stitch "$@"
