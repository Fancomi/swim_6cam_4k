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
rem
rem Args are positional and all optional: [backend] [duration_seconds] [nowindow]
rem   backend : d3d11 (default) | cudagl
rem   duration: positive integer seconds (default 30)
rem   nowindow: literal word to run without the preview window
rem
rem realtime mode paces at 30000/1001 fps (the design target); six 4K inputs are
rem dropped to that cadence by the latest-frame mailbox.

cd /d "%~dp0\.."
set "ROOT=%cd%"
set "EXE=%ROOT%\build\win-d3d11\Release\swim_realtime.exe"

rem --- Parse positional args (backend / duration / nowindow, any order) -------
set "BACKEND=d3d11"
set "DURATION=30"
set "PREVIEW=true"
for %%A in (%*) do (
  set "ARG=%%~A"
  if /I "!ARG!"=="d3d11" (
    set "BACKEND=d3d11"
  ) else if /I "!ARG!"=="cudagl" (
    set "BACKEND=cudagl"
  ) else if /I "!ARG!"=="nowindow" (
    set "PREVIEW=false"
  ) else (
    set "DURATION=!ARG!"
  )
)

if /I "%BACKEND%"=="cudagl" (
  set "CONFIG=%ROOT%\configs\windows_cudagl.conf"
) else (
  set "CONFIG=%ROOT%\configs\windows_20260629.conf"
)

if not exist "%EXE%" (
  echo Executable not found: "%EXE%"
  echo Build first: pwsh scripts\run_win.ps1 demo
  exit /b 1
)
if not exist "%CONFIG%" (
  echo Config not found: "%CONFIG%"
  exit /b 1
)

echo Running six-camera 4K realtime stitch [%BACKEND%]: window=%PREVIEW% duration=%DURATION%s
"%EXE%" --config "%CONFIG%" --stage=full --stream-count=6 --mode=realtime --duration-seconds=%DURATION% --preview=true "--preview-visible=%PREVIEW%" --encode=false "--metrics=%ROOT%\benchmarks\manual_%BACKEND%.jsonl"
echo Done. Metrics written to benchmarks\manual_%BACKEND%.jsonl
endlocal
