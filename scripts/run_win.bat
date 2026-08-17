@echo off
chcp 65001 >nul
goto :run

:说明
REM ==========================================================================
REM  Windows 实时拼接入口 -- 双击即可跑
REM
REM  六条相机线都从这一个 bat 进，底层都是 python -m python.stitch：
REM      pool        六路 4K 俯视泳池（旧 pool.fbx）
REM      pool2       同一批六路相机，新标注的 pool 1.fbx（当前默认）
REM      underwater  水下 16 块平面（同一条泳道，从下往上看）
REM      underwater2 同一批水下相机，重建的 8.15.fbx（15 块，去掉 A1）
REM      overhead    水上 2 块平面（同一条泳道，从上往下看）
REM      overhead2   同一批 overhead 相机，重建的 25 水面.fbx
REM  这里只把下面 EDITABLE 区的变量转成参数，不重复实现任何逻辑。
REM
REM  片段目录按机位分成 DIR_POOL / DIR_UNDER / DIR_OVER 三个变量：三个机位各自
REM  录到互不相干的目录里，一个默认值服务不了六条线。命令行给的路径优先。
REM
REM  要改什么，直接改 EDITABLE 区；也可以在命令行覆盖，命令行优先：
REM      scripts\run_win.bat                      用下面的默认值
REM      scripts\run_win.bat pool                 切回旧 pool.fbx
REM      scripts\run_win.bat under2               切到水下重建线（15 路）
REM      scripts\run_win.bat over2                切到俯视重建线
REM      scripts\run_win.bat under D:\SWIM\swb_x  水下 + 指定采样目录
REM      scripts\run_win.bat cudagl 600           换后端 + 跑 600 秒
REM      scripts\run_win.bat 60 nowindow          离屏，不开预览窗口
REM      scripts\run_win.bat noloop               片段放完就停
REM      scripts\run_win.bat encode               同时写出 HEVC
REM      scripts\run_win.bat fps:60               渲染 60fps（与输入帧率无关）
REM
REM  位置无关，按内容识别这些词：
REM      pool / pool2 / under / underwater / under2 / underwater2
REM      over / overhead / over2 / overhead2   哪条相机线
REM      d3d11 / cudagl      后端
REM      nowindow            离屏，不开预览窗口
REM      noloop              片段放完就停，不回到开头
REM      encode              写出 HEVC 文件
REM      fps:N               渲染帧率
REM      纯数字              秒数
REM      带 \ 或 / 的         片段目录
REM  完整开关：python -m python.stitch --help
REM
REM  片段放完默认回到开头继续播，所以秒数可以远超录制长度。
REM  片段目录要求：每台相机一个片段，水下 / 水上还需要 manifest.json。
REM  环境没装好先跑：scripts\install.bat
REM
REM  本文件 UTF-8 无 BOM + CRLF。中文只放在这个 goto 跳过的说明区里，:run
REM  之后的注释一律 ASCII——原因见 AGENTS.md「Windows bat 脚本编码规范」。
REM ==========================================================================

:run
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

rem ########################### EDITABLE begin ###############################

rem --- which camera line: pool, pool2, underwater, underwater2, overhead, overhead2 ---
set "LINE=pool2"

rem --- clip directories, one per physical rig; one clip per camera inside. ---
rem --- A command-line path overrides whichever one the line would pick.     ---
set "DIR_POOL=D:\WindowsProject\workspace\SWIM\20260730-4k-raw"
set "DIR_UNDER=D:\WindowsProject\workspace\SWIM\underwater2\swb_20260813-171923_26"
set "DIR_OVER=D:\WindowsProject\workspace\SWIM\overhead2\swb_20260730-160640_1"
set "VIDEO_DIR="

rem --- backend: d3d11 (Media Foundation) or cudagl (NVDEC + OpenGL) ---
set "BACKEND=d3d11"

rem --- seconds to run; clips loop, so a large value is fine ---
set "DURATION=30"

rem --- preview window: 1 shows it, 0 runs offscreen (still stitches on GPU) ---
set "WINDOW=1"

rem --- 1 rewinds clips at EOF, 0 stops there ---
set "LOOP=1"

rem --- 1 also writes HEVC to outputs\videos ---
set "ENCODE=0"

rem --- render fps; empty follows the config (30000/1001), or set e.g. 60 ---
set "FPS="

rem ########################### EDITABLE end #################################

rem Command-line args override the defaults above; order does not matter, each
rem token is recognised by its content.
for %%A in (%*) do (
  set "ARG=%%~A"
  if /I "!ARG!"=="pool" (
    set "LINE=pool"
  ) else if /I "!ARG!"=="pool2" (
    set "LINE=pool2"
  ) else if /I "!ARG!"=="under" (
    set "LINE=underwater"
  ) else if /I "!ARG!"=="underwater" (
    set "LINE=underwater"
  ) else if /I "!ARG!"=="under2" (
    set "LINE=underwater2"
  ) else if /I "!ARG!"=="underwater2" (
    set "LINE=underwater2"
  ) else if /I "!ARG!"=="over" (
    set "LINE=overhead"
  ) else if /I "!ARG!"=="overhead" (
    set "LINE=overhead"
  ) else if /I "!ARG!"=="over2" (
    set "LINE=overhead2"
  ) else if /I "!ARG!"=="overhead2" (
    set "LINE=overhead2"
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
    rem A token holding a path separator is the clip directory; anything else is
    rem the duration in seconds.
    echo !ARG! | findstr /R /C:"[\\/]" >nul 2>&1
    if errorlevel 1 (
      set "DURATION=!ARG!"
    ) else (
      set "VIDEO_DIR=!ARG!"
    )
  )
)

set "PY=%CD%\.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem No path on the command line: take the one belonging to this line's rig. The
rem three rigs record into unrelated directories, so a single default cannot
rem serve all six lines -- and profiles.py's own default points at the macOS
rem author's tree, which does not exist here.
if not defined VIDEO_DIR (
  if /I "!LINE!"=="pool"        set "VIDEO_DIR=!DIR_POOL!"
  if /I "!LINE!"=="pool2"       set "VIDEO_DIR=!DIR_POOL!"
  if /I "!LINE!"=="underwater"  set "VIDEO_DIR=!DIR_UNDER!"
  if /I "!LINE!"=="underwater2" set "VIDEO_DIR=!DIR_UNDER!"
  if /I "!LINE!"=="overhead"    set "VIDEO_DIR=!DIR_OVER!"
  if /I "!LINE!"=="overhead2"   set "VIDEO_DIR=!DIR_OVER!"
)

if defined VIDEO_DIR (
  if not exist "!VIDEO_DIR!\" (
    echo [ERROR] clip dir not found: "!VIDEO_DIR!"
    echo         pass one on the command line, or edit the DIR_* vars above.
    endlocal & exit /b 3
  )
)

rem All four steps go through one module, the same one macOS uses via
rem scripts/run_stitch.sh, so behaviour cannot drift between platforms.
set "ARGS=!LINE! extract,asset,build,live --seconds !DURATION! --backend !BACKEND!"
if defined VIDEO_DIR set "ARGS=!ARGS! --video-dir "!VIDEO_DIR!""
if "!WINDOW!"=="0"   set "ARGS=!ARGS! --no-window"
if "!LOOP!"=="0"     set "ARGS=!ARGS! --no-loop"
if "!ENCODE!"=="1"   set "ARGS=!ARGS! --encode"
if defined FPS       set "ARGS=!ARGS! --fps !FPS!"

echo !LINE! realtime stitch [!BACKEND!]: window=!WINDOW! loop=!LOOP! duration=!DURATION!s
if defined VIDEO_DIR echo   clips  : !VIDEO_DIR!
"%PY%" -m python.stitch !ARGS!
endlocal & exit /b %ERRORLEVEL%
