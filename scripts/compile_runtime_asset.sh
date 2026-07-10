#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p assets/generated
.venv/bin/python -m python.assets.compile_runtime_asset \
  outputs/data/pool_mesh.json assets/generated/pool_4k.swasset \
  --camera-ids cam3 cam2 cam1 cam4 cam5 cam6 --ppm 100
