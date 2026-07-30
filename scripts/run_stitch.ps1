# 相机拼接统一入口（Windows）—— 三条线共用这一个脚本。
#
#   pwsh scripts/run_stitch.ps1 LINE STEPS [选项…]
#
# LINE  pool | underwater | overhead
# STEPS extract,tex,still,video,asset,build,live 的任意子集，逗号分隔按序执行
#
# 例：
#   pwsh scripts/run_stitch.ps1 pool extract,still
#   pwsh scripts/run_stitch.ps1 overhead extract,asset,build,live --video-dir D:\SWIM\swb_x
#   pwsh scripts/run_stitch.ps1 underwater still --real
#
# 全部逻辑在 python/stitch/（与 macOS 共用同一份）。参数不在这里重新声明，否则
# 每加一个 CLI 选项都要改两处。双击入口见 scripts\run_win.bat。
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

function Resolve-Python {
  $venv = Join-Path $Root '.venv/Scripts/python.exe'
  if (Test-Path $venv) { return $venv }
  foreach ($name in @('python', 'python3', 'py')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
  }
  throw 'no Python interpreter found; run scripts\install.bat first'
}

$asking = $args.Count -ge 1 -and $args[0] -in @('--help', '-h', 'help')
if ($args.Count -lt 2 -or $asking) {
  # 说明只有一份：把本文件顶部的注释块打出来。
  Get-Content $PSCommandPath | Select-Object -First 13 |
    ForEach-Object { $_ -replace '^# ?', '' } | Write-Host
  Write-Host '完整选项：python -m python.stitch --help'
  # 显式求助是成功，参数不足是用法错误。
  exit $(if ($asking) { 0 } else { 2 })
}

$python = Resolve-Python
Push-Location $Root
try {
  & $python -m python.stitch @args
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
