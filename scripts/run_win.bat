@echo off
setlocal
rem Windows six-camera 4K realtime stitch demo (D3D11 + Media Foundation).
rem Usage:
rem   run_win.bat                 preview window, realtime ~30fps, 30 seconds
rem   run_win.bat 60              run for 60 seconds
rem   run_win.bat 30 nowindow     offscreen (still does real GPU stitch)
rem
rem realtime mode paces at 30000/1001 fps (the design target). Six 4K inputs are
rem dropped to that cadence by the latest-frame mailbox. For the unthrottled GPU
rem throughput ceiling, see benchmark mode in README (stage 2).

cd /d "%~dp0\.."
set "ROOT=%cd%"
set "EXE=%ROOT%\build\win-d3d11\Release\swim_realtime.exe"
set "CONFIG=%ROOT%\configs\windows_20260629.conf"
set "DURATION=%~1"
if "%DURATION%"=="" set "DURATION=30"

set "PREVIEW=true"
if /I "%~2"=="nowindow" set "PREVIEW=false"

if not exist "%EXE%" (
  echo Executable not found. Build first:
  echo   pwsh scripts\run_win.ps1 demo
  exit /b 1
)

echo Running six-camera 4K realtime stitch: window=%PREVIEW% duration=%DURATION%s
"%EXE%" --config "%CONFIG%" --stage=full --stream-count=6 --mode=realtime --duration-seconds=%DURATION% --preview=true "--preview-visible=%PREVIEW%" --encode=false "--metrics=%ROOT%\benchmarks\manual.jsonl"
echo Done. Metrics written to benchmarks\manual.jsonl
endlocal
