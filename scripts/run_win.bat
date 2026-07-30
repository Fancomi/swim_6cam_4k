@echo off
chcp 65001 >nul
goto :run

:说明
REM ==========================================================================
REM  六路 4K 实时拼接 -- 双击即可跑
REM
REM  真正的逻辑全在 scripts\run_win.ps1 里，这个文件只负责把下面 EDITABLE 区
REM  的变量转成参数交给它，不重复实现任何东西。
REM
REM  要改什么，直接改 EDITABLE 区；也可以在命令行覆盖，命令行优先：
REM      scripts\run_win.bat                     用下面的默认值
REM      scripts\run_win.bat cudagl              换后端
REM      scripts\run_win.bat cudagl 600          换后端 + 跑 600 秒
REM      scripts\run_win.bat d3d11 60 nowindow   离屏，不开预览窗口
REM      scripts\run_win.bat d3d11 60 noloop     片段放完就停
REM      scripts\run_win.bat cudagl fps:60       渲染 60fps（与输入帧率无关）
REM  位置无关，认这几个词：d3d11 / cudagl / nowindow / noloop / fps:N /
REM  纯数字（秒数）。更多开关直接用 ps1：pwsh scripts\run_win.ps1 -?
REM
REM  片段放完默认回到开头继续播，所以秒数可以远超录制长度。
REM  水下 16 路是另一条链路：scripts\run_underwater.ps1
REM  环境没装好先跑：scripts\install.bat
REM
REM  本文件 UTF-8 无 BOM + CRLF，中文只放在这个 goto 跳过的说明区里，
REM  :run 之后的注释一律 ASCII。原因见 AGENTS.md「Windows bat 脚本编码规范」。
REM ==========================================================================

:run
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

rem ########################### EDITABLE begin ###############################

rem --- backend: d3d11 (Media Foundation) or cudagl (NVDEC + OpenGL) ---
set "BACKEND=d3d11"

rem --- seconds to run; clips loop, so a large value is fine ---
set "DURATION=30"

rem --- preview window: 1 shows it, 0 runs offscreen (still stitches on GPU) ---
set "WINDOW=1"

rem --- 1 rewinds clips at EOF, 0 stops there ---
set "LOOP=1"

rem --- render fps; empty follows the config (30000/1001), or set e.g. 60 ---
set "FPS="

rem ########################### EDITABLE end #################################

rem Command-line args override the defaults above; order does not matter, each
rem token is recognised by its content.
for %%A in (%*) do (
  set "ARG=%%~A"
  if /I "!ARG!"=="d3d11" (
    set "BACKEND=d3d11"
  ) else if /I "!ARG!"=="cudagl" (
    set "BACKEND=cudagl"
  ) else if /I "!ARG!"=="nowindow" (
    set "WINDOW=0"
  ) else if /I "!ARG!"=="noloop" (
    set "LOOP=0"
  ) else if /I "!ARG:~0,4!"=="fps:" (
    set "FPS=!ARG:~4!"
  ) else (
    set "DURATION=!ARG!"
  )
)

set "PS_ARGS=-Backend !BACKEND! -Duration !DURATION!"
if "!WINDOW!"=="0"  set "PS_ARGS=!PS_ARGS! -NoWindow"
if "!LOOP!"=="0"    set "PS_ARGS=!PS_ARGS! -NoLoop"
if defined FPS      set "PS_ARGS=!PS_ARGS! -Fps !FPS!"

rem Prefer pwsh (7+), fall back to the built-in powershell 5.1; both run the ps1.
set "PS=pwsh"
where pwsh >nul 2>&1 || set "PS=powershell"

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_win.ps1" !PS_ARGS!
endlocal & exit /b %ERRORLEVEL%
