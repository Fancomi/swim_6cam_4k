# Windows D3D11 实时路径入口脚本（第一阶段：demo）。
#
# 用法:
#   pwsh scripts/run_win.ps1 demo [-Duration 30] [-NoWindow] [-Config PATH] [-BuildDir PATH]
#
# 对应 macOS 的 scripts/run_metal.sh demo。第一阶段只提供 demo；
# benchmark / soak 待第二阶段补齐。
[CmdletBinding()]
param(
  [Parameter(Position = 0)]
  [string]$Command = 'demo',
  [int]$Duration = 30,
  [switch]$NoWindow,
  [switch]$NoEncode,
  [string]$Config,
  [string]$BuildDir,
  [string]$Executable,
  [string]$Stage = 'full',
  [int]$StreamCount = 6
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Config) { $Config = Join-Path $Root 'configs/windows_20260629.conf' }
if (-not $BuildDir) { $BuildDir = Join-Path $Root 'build/win-d3d11' }

function Resolve-Python {
  $venv = Join-Path $Root '.venv/Scripts/python.exe'
  if (Test-Path $venv) { return $venv }
  $py = Get-Command python -ErrorAction SilentlyContinue
  if ($py) { return $py.Source }
  throw 'no Python interpreter found (create .venv with numpy+opencv first)'
}

function Ensure-Executable {
  if ($script:Executable) { return }
  $python = Resolve-Python
  # Compile the runtime asset if it is missing (build step depends on numpy+cv2).
  $asset = Join-Path $Root 'assets/generated/pool_4k.swasset'
  if (-not (Test-Path $asset)) {
    & $python -m python.assets.compile_runtime_asset `
      (Join-Path $Root 'outputs/data/pool_mesh.json') $asset `
      --camera-ids cam3 cam2 cam1 cam4 cam5 cam6 --ppm 100
  }
  & cmake -S $Root -B $BuildDir -G 'Visual Studio 17 2022' -A x64 `
    "-DPython3_EXECUTABLE=$python"
  & cmake --build $BuildDir --config Release --target swim_realtime
  $script:Executable = Join-Path $BuildDir 'Release/swim_realtime.exe'
}

function Invoke-Demo {
  Ensure-Executable
  if (-not (Test-Path $Config)) { throw "config does not exist: $Config" }
  $metrics = Join-Path $Root 'benchmarks/manual.jsonl'
  New-Item -ItemType Directory -Force -Path (Split-Path $metrics) | Out-Null

  $previewVisible = if ($NoWindow) { 'false' } else { 'true' }
  $args = @(
    '--config', $Config,
    "--stage=$Stage",
    "--stream-count=$StreamCount",
    '--mode=realtime',
    "--duration-seconds=$Duration",
    '--preview=true',
    "--preview-visible=$previewVisible"
  )
  if ($NoEncode) {
    $args += @('--encode=false')
  } else {
    # First-stage HEVC encode is not yet implemented on the D3D11 backend.
    $args += @('--encode=false')
  }
  $args += @("--metrics=$metrics")

  Write-Host "D3D11 demo: window=$(-not $NoWindow) duration=${Duration}s"
  Write-Host "  executable: $script:Executable"
  Write-Host "  metrics: $metrics"
  & $script:Executable @args
  Write-Host 'D3D11 demo complete.'
}

switch ($Command) {
  'demo' { Invoke-Demo }
  default { throw "unknown command: $Command (only 'demo' is supported in stage 1)" }
}
