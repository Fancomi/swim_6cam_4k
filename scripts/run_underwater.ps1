# 水下 16 路实时拼接一键脚本（Windows）。
#
# 用法:
#   pwsh scripts/run_underwater.ps1 VIDEO_DIR [选项…]
#   pwsh scripts/run_underwater.ps1 D:\SWIM\swb_x -Seconds 30 -Encode
#
# 所有实际逻辑都在 python/underwater/run.py 里（与 macOS 共用同一份），这个脚本
# 只负责挑选解释器并转发参数。run.py 自己会选 Visual Studio 生成器和 d3d11 后端。
[CmdletBinding()]
param(
  [Parameter(Position = 0, Mandatory = $true)]
  [string]$VideoDir,
  [int]$Seconds = 30,
  [int]$Fps = 0,
  [switch]$Encode,
  [switch]$NoWindow,
  [switch]$NoPreview,
  [switch]$Force,
  [string]$Backend,
  [string]$Steps,
  [string]$Config,
  [string]$EncodePath,
  [string]$Metrics
)

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
$forward = @('-m', 'python.stitch.run', '--video-dir', $VideoDir,
             '--seconds', $Seconds)
if ($Fps -gt 0)      { $forward += @('--fps', $Fps) }
if ($Encode)         { $forward += '--encode' }
if ($NoWindow)       { $forward += '--no-window' }
if ($NoPreview)      { $forward += '--no-preview' }
if ($Force)          { $forward += '--force' }
if ($Backend)        { $forward += @('--backend', $Backend) }
if ($Steps)          { $forward += @('--steps', $Steps) }
if ($Config)         { $forward += @('--config', $Config) }
if ($EncodePath)     { $forward += @('--encode-path', $EncodePath) }
if ($Metrics)        { $forward += @('--metrics', $Metrics) }

Push-Location $Root
try {
  & $python @forward
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
