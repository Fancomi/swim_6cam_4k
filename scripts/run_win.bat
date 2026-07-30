@echo off
chcp 65001 >nul
goto :run

:说明
REM ==========================================================================
REM  Windows 实时拼接入口 -- 双击即可跑
REM
REM  两条链路共用这一个 bat：
REM      六路 4K  -> scripts\run_6cam_4k.ps1
REM      水下 16 路 -> python -m python.underwater.run
REM  这里只把下面 EDITABLE 区的变量转成参数，不重复实现任何逻辑。
REM
REM  要改什么，直接改 EDITABLE 区；也可以在命令行覆盖，命令行优先：
REM      scripts\run_win.bat                        用下面的默认值（六路 4K）
REM      scripts\run_win.bat cudagl                 换后端
REM      scripts\run_win.bat cudagl 600             换后端 + 跑 600 秒
REM      scripts\run_win.bat d3d11 60 nowindow      离屏，不开预览窗口
REM      scripts\run_win.bat d3d11 60 noloop        片段放完就停
REM      scripts\run_win.bat cudagl fps:60          渲染 60fps（与输入帧率无关）
REM
REM      scripts\run_win.bat under                  水下 16 路，用下面的采样目录
REM      scripts\run_win.bat under D:\SWIM\swb_x    水下 16 路，指定采样目录
REM      scripts\run_win.bat under 20 nowindow      水下 + 跑 20 秒 + 离屏
REM      scripts\run_win.bat under encode           水下 + 同时写出 HEVC
REM
REM  位置无关，按内容识别这些词：
REM      under / underwater  切到水下 16 路（默认是六路 4K）
REM      d3d11 / cudagl      后端
REM      nowindow            离屏，不开预览窗口
REM      noloop              片段放完就停，不回到开头
REM      encode              写出 HEVC 文件（仅水下链路支持）
REM      fps:N               渲染帧率
REM      纯数字              秒数
REM      带 \ 或 / 的         水下采样目录
REM  更多开关直接调底层：pwsh scripts\run_6cam_4k.ps1 -?
REM
REM  片段放完默认回到开头继续播，所以秒数可以远超录制长度。
REM  水下采样目录必须含 16 个 *_underAi.ts 与 manifest.json。
REM  环境没装好先跑：scripts\install.bat
REM
REM  本文件 UTF-8 无 BOM + CRLF。中文只放在这个 goto 跳过的说明区里，:run
REM  之后的注释一律 ASCII——原因见 AGENTS.md「Windows bat 脚本编码规范」。
REM ==========================================================================

:run
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

rem ########################### EDITABLE begin ###############################

rem --- which pipeline: 6cam (six-camera 4K) or under (underwater 16-lane) ---
set "MODE=6cam"

rem --- underwater sample dir: 16 *_underAi.ts plus manifest.json ---
set "SAMPLE_DIR=D:\WindowsProject\workspace\SWIM\under-xlj-all\swb_20260728-150356_6"

rem --- backend: d3d11 (Media Foundation) or cudagl (NVDEC + OpenGL) ---
set "BACKEND=d3d11"

rem --- seconds to run; clips loop, so a large value is fine ---
set "DURATION=30"

rem --- preview window: 1 shows it, 0 runs offscreen (still stitches on GPU) ---
set "WINDOW=1"

rem --- 1 rewinds clips at EOF, 0 stops there ---
set "LOOP=1"

rem --- 1 also writes HEVC to outputs\videos (underwater pipeline only) ---
set "ENCODE=0"

rem --- render fps; empty follows the config (30000/1001), or set e.g. 60 ---
set "FPS="

rem ########################### EDITABLE end #################################

rem Command-line args override the defaults above; order does not matter, each
rem token is recognised by its content.
for %%A in (%*) do (
  set "ARG=%%~A"
  if /I "!ARG!"=="under" (
    set "MODE=under"
  ) else if /I "!ARG!"=="underwater" (
    set "MODE=under"
  ) else if /I "!ARG!"=="6cam" (
    set "MODE=6cam"
  ) else if /I "!ARG!"=="d3d11" (
    set "BACKEND=d3d11"
  ) else if /I "!ARG!"=="cudagl" (
    set "BACKEND=cudagl"
  ) else if /I "!ARG!"=="nowindow" (
    set "WINDOW=0"
  ) else if /I "!ARG!"=="noloop" (
    set "LOOP=0"
  ) else if /I "!ARG!"=="encode" (
    set "ENCODE=1"
  ) else if /I "!ARG:~0,4!"=="fps:" (
    set "FPS=!ARG:~4!"
  ) else (
    rem A token holding a path separator is the underwater sample directory;
    rem anything else is the duration in seconds.
    echo !ARG! | findstr /R /C:"[\\/]" >nul 2>&1
    if errorlevel 1 (
      set "DURATION=!ARG!"
    ) else (
      set "SAMPLE_DIR=!ARG!"
      set "MODE=under"
    )
  )
)

if /I "!MODE!"=="under" goto :run_underwater

rem --- six-camera 4K: all logic lives in run_6cam_4k.ps1 -------------------
set "PS_ARGS=-Backend !BACKEND! -Duration !DURATION!"
if "!WINDOW!"=="0"  set "PS_ARGS=!PS_ARGS! -NoWindow"
if "!LOOP!"=="0"    set "PS_ARGS=!PS_ARGS! -NoLoop"
if defined FPS      set "PS_ARGS=!PS_ARGS! -Fps !FPS!"
if "!ENCODE!"=="1"  echo [warn] encode is underwater-only; ignored for 6cam

rem Prefer pwsh (7+), fall back to the built-in powershell 5.1; both work.
set "PS=pwsh"
where pwsh >nul 2>&1 || set "PS=powershell"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_6cam_4k.ps1" !PS_ARGS!
endlocal & exit /b %ERRORLEVEL%

rem --- underwater 16 lanes: python.underwater.run drives all four steps ----
rem     Same module macOS uses via scripts/run_underwater.sh, so the extract /
rem     asset / build / run behaviour cannot drift between platforms.
:run_underwater
set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if not exist "!SAMPLE_DIR!\" (
  echo [ERROR] sample dir not found: "!SAMPLE_DIR!"
  echo         pass one on the command line, or edit SAMPLE_DIR above.
  endlocal & exit /b 3
)

set "PY_ARGS=--video-dir "!SAMPLE_DIR!" --seconds !DURATION! --backend !BACKEND!"
if "!WINDOW!"=="0"  set "PY_ARGS=!PY_ARGS! --no-window"
if "!LOOP!"=="0"    set "PY_ARGS=!PY_ARGS! --no-loop"
if "!ENCODE!"=="1"  set "PY_ARGS=!PY_ARGS! --encode"
if defined FPS      set "PY_ARGS=!PY_ARGS! --fps !FPS!"

echo underwater 16-lane stitch [!BACKEND!]: window=!WINDOW! loop=!LOOP! duration=!DURATION!s
echo   sample : !SAMPLE_DIR!
"%PY%" -m python.underwater.run !PY_ARGS!
endlocal & exit /b %ERRORLEVEL%
