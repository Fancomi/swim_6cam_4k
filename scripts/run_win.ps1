# Windows 六路 4K 实时拼接入口（d3d11 / cudagl 两个后端共用）。
#
# 这里是唯一的逻辑所在：解析参数、挑后端与 config、必要时构建、然后跑 exe。
# scripts\run_win.bat 只是它的 cmd 包装（双击友好），不复制任何逻辑。
#
# 用法:
#   pwsh scripts/run_win.ps1                      # d3d11，预览窗口，30 秒
#   pwsh scripts/run_win.ps1 -Backend cudagl
#   pwsh scripts/run_win.ps1 -Duration 600        # 片段会自动循环重播
#   pwsh scripts/run_win.ps1 -NoWindow            # 离屏（仍执行真实 GPU 拼接）
#   pwsh scripts/run_win.ps1 -Fps 60              # 渲染帧率，与输入帧率无关
#   pwsh scripts/run_win.ps1 -NoLoop              # 片段放完就停
#
# 片段放完默认回到开头继续播，所以 -Duration 可以远超录制长度；加 -NoLoop 时
# 时长超过最短那路片段会以 "MP4 reached EOF before global render deadline" 失败。
#
# 水下 16 路是另一条链路，入口是 scripts/run_underwater.ps1。
# 环境安装见 scripts/install.bat。
[CmdletBinding()]
param(
  [ValidateSet('d3d11', 'cudagl')]
  [string]$Backend = 'd3d11',
  [ValidateRange(1, 86400)]
  [int]$Duration = 30,
  # 0 表示沿用 config 里的 fps_num/fps_den（默认 30000/1001）。
  [ValidateRange(0, 1000)]
  [int]$Fps = 0,
  [switch]$NoWindow,
  [switch]$NoLoop,
  # 即使 exe 已存在也重新配置并构建。
  [switch]$Rebuild,
  [string]$Config,
  [string]$BuildDir,
  [string]$Executable,
  [string]$Metrics,
  [string]$Stage = 'full',
  [ValidateRange(1, 16)]
  [int]$StreamCount = 6
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

# Configs moved under inputs/configs/ in 64e05d8 "move runtime config into
# inputs/configs"; each backend names itself in its own file via `backend=`.
if (-not $Config) {
  $Config = Join-Path $Root "inputs/configs/windows_$(
    if ($Backend -eq 'cudagl') { 'cudagl' } else { '20260629' }).conf"
}
# One build tree per backend, matching scripts\install.bat and
# python.underwater.run (run.py: build_dir_for).
if (-not $BuildDir) { $BuildDir = Join-Path $Root "build/win-$Backend" }
if (-not $Metrics)  { $Metrics  = Join-Path $Root "benchmarks/manual_$Backend.jsonl" }

function Resolve-Python {
  $venv = Join-Path $Root '.venv/Scripts/python.exe'
  if (Test-Path $venv) { return $venv }
  $py = Get-Command python -ErrorAction SilentlyContinue
  if ($py) { return $py.Source }
  throw 'no Python interpreter found; run scripts\install.bat first'
}

function Resolve-Executable {
  if ($Executable) { return $Executable }
  $exe = Join-Path $BuildDir 'Release/swim_realtime.exe'
  if ((Test-Path $exe) -and -not $Rebuild) { return $exe }

  # CMake compiles the .swasset itself (runtime_asset is a dependency of
  # swim_realtime), so building is the only step needed — but it reads a mesh
  # JSON that install.bat generates and .gitignore excludes.
  $mesh = Join-Path $Root 'outputs/data/pool_mesh.json'
  if (-not (Test-Path $mesh)) {
    throw "missing $mesh — run scripts\install.bat to generate it"
  }
  $python = Resolve-Python
  Write-Host "building $Backend (Release)..."
  # Out-Host, not the default output stream: anything a function writes to
  # output becomes part of its return value, so unredirected cmake logs would
  # come back alongside the path and Test-Path would choke on the array.
  & cmake -S $Root -B $BuildDir -G 'Visual Studio 17 2022' -A x64 `
    "-DPython3_EXECUTABLE=$python" | Out-Host
  if ($LASTEXITCODE -ne 0) { throw 'cmake configure failed' }
  & cmake --build $BuildDir --config Release --target swim_realtime | Out-Host
  if ($LASTEXITCODE -ne 0) { throw 'cmake build failed' }
  Copy-RuntimeDlls (Join-Path $BuildDir 'Release')
  return $exe
}

# CMake has no copy rules for the CUDA/GL backend's shared libraries, and the
# exe links every backend that was available at configure time — so a tree
# without these DLLs fails to load with 0xC0000135 no matter which backend the
# run selects. scripts\install.bat does the same for the trees it builds; this
# keeps a freshly built tree (or a custom -BuildDir) runnable on its own.
function Copy-RuntimeDlls([string]$Destination) {
  $sources = @()
  $ffmpegBin = Join-Path $Root 'third_party/ffmpeg/bin'
  if (Test-Path $ffmpegBin) {
    foreach ($lib in 'avcodec', 'avformat', 'avutil', 'swresample', 'swscale') {
      $sources += Get-ChildItem -Path $ffmpegBin -Filter "$lib-*.dll" -ErrorAction SilentlyContinue
    }
  }
  $glfw = Join-Path $Root 'third_party/glfw/lib-vc2022/glfw3.dll'
  if (Test-Path $glfw) { $sources += Get-Item $glfw }
  $cudaRoot = if ($env:CUDA_PATH) { $env:CUDA_PATH }
              else { 'C:/Program Files/NVIDIA GPU Computing Toolkit/CUDA/v12.8' }
  $cudart = Join-Path $cudaRoot 'bin/cudart64_12.dll'
  if (Test-Path $cudart) { $sources += Get-Item $cudart }

  foreach ($file in $sources) {
    $target = Join-Path $Destination $file.Name
    if (-not (Test-Path $target)) { Copy-Item $file.FullName $target -Force }
  }
  if ($sources.Count -eq 0) {
    Write-Host '  [warn] no third_party/CUDA DLLs found; run scripts\install.bat'
  }
}

$exe = Resolve-Executable
if (-not (Test-Path $exe))    { throw "executable does not exist: $exe" }
if (-not (Test-Path $Config)) { throw "config does not exist: $Config" }
New-Item -ItemType Directory -Force -Path (Split-Path $Metrics) | Out-Null

$arguments = @(
  '--config', $Config,
  "--stage=$Stage",
  "--stream-count=$StreamCount",
  '--mode=realtime',
  "--duration-seconds=$Duration",
  '--preview=true',
  "--preview-visible=$(if ($NoWindow) { 'false' } else { 'true' })",
  "--loop=$(if ($NoLoop) { 'false' } else { 'true' })",
  # Hardware HEVC encode is not implemented on the Windows backends yet.
  '--encode=false',
  "--metrics=$Metrics"
)
if ($Fps -gt 0) { $arguments += "--fps=$Fps" }

Write-Host ("six-camera 4K realtime stitch [{0}]: window={1} loop={2} duration={3}s fps={4}" -f `
  $Backend, (-not $NoWindow), (-not $NoLoop), $Duration,
  $(if ($Fps -gt 0) { $Fps } else { 'config' }))
Write-Host "  exe    : $exe"
Write-Host "  config : $Config"
Write-Host "  metrics: $Metrics"

& $exe @arguments
$code = $LASTEXITCODE
if ($code -ne 0) { Write-Host "exit=$code"; exit $code }
Write-Host "done. metrics -> $Metrics"
