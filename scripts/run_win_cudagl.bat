@echo off
setlocal
rem Windows six-camera 4K realtime stitch demo, CUDA/GL backend.
rem NVDEC (FFmpeg h264_cuvid) decode + CUDA-GL interop upload + OpenGL stitch +
rem GLFW preview. Requires the FFmpeg/GLFW/CUDA runtime DLLs beside the exe
rem (the build copies them; if missing, rerun the CUDA/GL CMake build).
rem Usage:
rem   run_win_cudagl.bat                 preview window, ~30fps, 30 seconds
rem   run_win_cudagl.bat 60              run for 60 seconds
rem   run_win_cudagl.bat 30 nowindow     offscreen (still does real GPU stitch)

cd /d "%~dp0\.."
set "ROOT=%cd%"
set "EXE=%ROOT%\build\win-d3d11\Release\swim_realtime.exe"
set "CONFIG=%ROOT%\configs\windows_cudagl.conf"
set "DURATION=%~1"
if "%DURATION%"=="" set "DURATION=30"

set "PREVIEW=true"
if /I "%~2"=="nowindow" set "PREVIEW=false"

if not exist "%EXE%" (
  echo Executable not found. Build first with the CUDA/GL backend enabled.
  exit /b 1
)

echo Running six-camera 4K realtime stitch (CUDA/GL): window=%PREVIEW% duration=%DURATION%s
"%EXE%" --config "%CONFIG%" --stage=full --stream-count=6 --mode=realtime --duration-seconds=%DURATION% --preview=true "--preview-visible=%PREVIEW%" --encode=false "--metrics=%ROOT%\benchmarks\manual_cudagl.jsonl"
echo Done. Metrics written to benchmarks\manual_cudagl.jsonl
endlocal
