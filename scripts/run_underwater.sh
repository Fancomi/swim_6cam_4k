#!/usr/bin/env bash
# 水下 16 路实时拼接一键脚本（macOS / Linux）。
#
# 用法:
#   ./scripts/run_underwater.sh VIDEO_DIR [选项…]
#   ./scripts/run_underwater.sh VIDEO_DIR --seconds 30 --encode
#
# 所有实际逻辑都在 python/underwater/run.py 里（mac 与 Windows 共用同一份），
# 这个脚本只负责挑选解释器并把参数原样转发。Windows 用 scripts/run_underwater.ps1。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

if (($# == 0)); then
  cat >&2 <<'EOF'
Usage: scripts/run_underwater.sh VIDEO_DIR [options]

VIDEO_DIR 必须含 16 个 *_underAi.ts 片段（以及 manifest.json）。

Common options (full list: --help):
  --seconds N        运行秒数（默认 30）
  --encode           同时写出 HEVC 文件
  --no-window        离屏渲染，不开预览窗口
  --no-loop          片段放完就停（默认回到开头继续播，可跑任意长时间）
  --fps N            覆盖渲染帧率（默认跟随片段）
  --steps LIST       只跑部分步骤，如 asset,run
  --force            即使产物是新的也重做
  --backend NAME     metal / d3d11 / cudagl（默认按平台）
EOF
  exit 2
fi

# 第一个位置参数是片段目录；其余原样转发。
VIDEO_DIR="$1"
shift
exec "$PY" -m python.underwater.run --video-dir "$VIDEO_DIR" "$@"
