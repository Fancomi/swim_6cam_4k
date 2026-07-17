@echo off
setlocal enabledelayedexpansion
rem Windows six-camera 4K realtime stitch demo. One entry point for both GPU
rem backends; picks the config and prints live render/decode/preview FPS.
rem
rem Usage:
rem   run_win.bat                        d3d11 backend, preview window, 30 s
rem   run_win.bat cudagl                 CUDA/GL (NVDEC+OpenGL) backend
rem   run_win.bat d3d11 60               run for 60 seconds
rem   run_win.bat cudagl 60 nowindow     offscreen (still does the real GPU stitch)
rem   run_win.bat cudagl fps:60          render at 60 fps (data-independent)
rem
rem Args are positional and all optional: [backend] [duration_seconds] [nowindow] [fps:N]
rem   backend : d3d11 (default) | cudagl
rem   duration: positive integer seconds (default 30)
rem   nowindow: literal word to run without the preview window
rem   fps:N   : render cadence target in fps (default: config's 30000/1001)
rem
rem realtime mode paces the renderer at the target fps; six 4K inputs are reused
rem or dropped by the latest-frame mailbox to meet it, independent of clip fps.

cd /d "%~dp0\.."
set "ROOT=%cd%"
set "EXE=%ROOT%\build\win-d3d11\Release\swim_realtime.exe"

rem --- Parse positional args (backend / duration / nowindow / fps:N, any order)
set "BACKEND=d3d11"
set "DURATION=30"
set "PREVIEW=true"
set "FPS="
for %%A in (%*) do (
  set "ARG=%%~A"
  if /I "!ARG!"=="d3d11" (
    set "BACKEND=d3d11"
  ) else if /I "!ARG!"=="cudagl" (
    set "BACKEND=cudagl"
  ) else if /I "!ARG!"=="nowindow" (
    set "PREVIEW=false"
  ) else if /I "!ARG:~0,4!"=="fps:" (
    set "FPS=!ARG:~4!"
  ) else (
    set "DURATION=!ARG!"
  )
)

if /I "%BACKEND%"=="cudagl" (
  set "CONFIG=%ROOT%\configs\windows_cudagl.conf"
) else (
  set "CONFIG=%ROOT%\configs\windows_20260629.conf"
)

set "FPS_ARG="
if defined FPS set "FPS_ARG=--fps=%FPS%"

if not exist "%EXE%" (
  echo Executable not found: "%EXE%"
  echo Build first: pwsh scripts\run_win.ps1 demo
  exit /b 1
)
if not exist "%CONFIG%" (
  echo Config not found: "%CONFIG%"
  exit /b 1
)

echo Running six-camera 4K realtime stitch [%BACKEND%]: window=%PREVIEW% duration=%DURATION%s fps=%FPS%
"%EXE%" --config "%CONFIG%" --stage=full --stream-count=6 --mode=realtime --duration-seconds=%DURATION% --preview=true "--preview-visible=%PREVIEW%" --encode=false %FPS_ARG% "--metrics=%ROOT%\benchmarks\manual_%BACKEND%.jsonl"
echo Done. Metrics written to benchmarks\manual_%BACKEND%.jsonl
endlocal
