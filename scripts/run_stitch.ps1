# 平面拼接统一入口（Windows）。
#
# 用法:
#   pwsh scripts/run_stitch.ps1 PROFILE STEPS [选项…]
#   pwsh scripts/run_stitch.ps1 overhead extract,still
#   pwsh scripts/run_stitch.ps1 underwater extract,asset,build,live -- --video-dir D:\SWIM\swb_x
#
# 全部逻辑在 python/stitch/__main__.py（与 macOS 共用同一份）。这个脚本只挑选
# 解释器并原样转发参数 —— 参数不在这里重新声明，否则每加一个 CLI 选项都要改两处。
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

function Resolve-Python {
  $venv = Join-Path $Root '.venv/Scripts/python.exe'
  if (Test-Path $venv) { return $venv }
  foreach ($name in @('python', 'python3', 'py')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
  }
  throw 'no Python interpreter found (create .venv with numpy+opencv first)'
}

$python = Resolve-Python
Push-Location $Root
try {
  & $python -m python.stitch @args
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
