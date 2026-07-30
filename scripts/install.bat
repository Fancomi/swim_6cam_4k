@echo off
chcp 65001 >nul
goto :run

:说明
REM ==========================================================================
REM  swim_6cam_4k -- Windows 一键环境安装
REM
REM  把 C++ 与 Python 两侧全部拉通，装完这些命令直接可用：
REM      pwsh scripts\run_underwater.ps1 <采样目录>    水下 16 路实时拼接
REM      scripts\run_win.bat                           六路 4K 实时拼接
REM
REM  用法：
REM      scripts\install.bat            核心环境
REM      scripts\install.bat pose       追加 torch/ultralytics（约 2.5GB，仅入水检测）
REM      scripts\install.bat check      只体检，不改动任何东西
REM
REM  每一步都幂等：已就绪就打印 [SKIP]，可以反复跑。任一步失败立即停下，
REM  并打印可直接执行的修复命令。
REM
REM  七步：C++ 工具链体检 -> winget 装 Python 3.10 -> 建/重建 .venv ->
REM  装 requirements-win.txt -> 下载并安装 FBX Python SDK（官方 GUI 安装器，
REM  一路默认即可）-> 拉取 third_party 的 FFmpeg+GLFW 并体检 CUDA ->
REM  生成网格与 .swasset、构建 swim_realtime.exe、把运行期 DLL 拷到 exe 旁。
REM
REM  刻意不自动装 Visual Studio 2022 与 CUDA Toolkit：体积大、要重启、要选
REM  组件，只体检并给出 winget 命令。缺 CUDA 只是警告，此时 cudagl 后端会被
REM  跳过，d3d11 后端不受影响。
REM
REM  本文件 UTF-8 无 BOM + CRLF。中文只放在这个 goto 跳过的说明区里，:run
REM  之后的注释一律 ASCII——原因见 AGENTS.md「Windows bat 脚本编码规范」。
REM ==========================================================================

:run
setlocal EnableDelayedExpansion

cd /d "%~dp0.."
set "ROOT=%cd%"

set "MODE=install"
if /I "%~1"=="check" set "MODE=check"
if /I "%~1"=="pose"  set "MODE=pose"

set "VENV=%ROOT%\.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "BUILD=%ROOT%\build\win-d3d11"
set "EXE=%BUILD%\Release\swim_realtime.exe"
set "DOWNLOADS=%ROOT%\third_party\downloads"

set "FBX_URL=https://damassets.autodesk.net/content/dam/autodesk/www/files/fbx202037_fbxpythonsdk_win.exe"
set "FBX_EXE=%DOWNLOADS%\fbx202037_fbxpythonsdk_win.exe"
set "FBX_BYTES=2865256"
set "FBX_ROOT=C:\Program Files\Autodesk\FBX\FBX Python SDK"

set "FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl-shared.zip"
set "GLFW_URL=https://github.com/glfw/glfw/releases/download/3.4/glfw-3.4.bin.WIN64.zip"

set "CUDA_DIR=%CUDA_PATH%"
if not defined CUDA_DIR set "CUDA_DIR=C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.8"

set "FAILED="
set "WARNED="

echo.
echo ==========================================================
echo   swim_6cam_4k Windows 环境安装   [模式: %MODE%]
echo   项目根目录: %ROOT%
echo ==========================================================

call :step1_toolchain          || goto :fatal
call :step2_python310          || goto :fatal
call :step3_venv               || goto :fatal
call :step4_pydeps             || goto :fatal
call :step5_fbxsdk             || goto :fatal
call :step6_thirdparty         || goto :fatal
call :step7_buildall           || goto :fatal

goto :summary


rem ======================================================================
rem  Step 1 -- C++ toolchain check (read-only, installs nothing)
rem ======================================================================
:step1_toolchain
echo.
echo --- [1/7] C++ 工具链 -------------------------------------
rem The parentheses in %ProgramFiles(x86)% truncate an if/for block when the
rem variable expands inside one, so hoist it into a plain variable first.
set "PFX86=%ProgramFiles(x86)%"
set "VSWHERE=%PFX86%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VSPATH="
rem Pin 17.x: Step 7 hardcodes the "Visual Studio 17 2022" generator, and
rem without a version range vswhere picks the newer VS18 BuildTools instead.
rem Via a temp file, not for /f backquotes: the ")" in the version range is
rem eaten by cmd paren parsing first, so the backquote form never runs.
if exist "%VSWHERE%" (
  "%VSWHERE%" -products * -version "[17.0,18.0)" -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath > "%TEMP%\swim_vs.txt" 2>nul
  for /f "usebackq delims=" %%P in ("%TEMP%\swim_vs.txt") do set "VSPATH=%%P"
  del "%TEMP%\swim_vs.txt" >nul 2>&1
)
if not defined VSPATH (
  echo   [FAIL] 没找到带 C++ 工作负载的 Visual Studio 2022
  echo          几个 GB 且要重启, 所以本脚本不自动装. 手工执行:
  echo          winget install Microsoft.VisualStudio.2022.Community --override "--add Microsoft.VisualStudio.Workload.NativeDesktop"
  exit /b 1
)
echo   [OK]   Visual Studio 2022: %VSPATH%

where cmake >nul 2>&1
if errorlevel 1 (
  echo   [FAIL] cmake 不在 PATH
  echo          winget install Kitware.CMake
  exit /b 1
)
cmake --version > "%TEMP%\swim_cmake.txt" 2>nul
for /f "usebackq tokens=3" %%V in ("%TEMP%\swim_cmake.txt") do (
  if not defined CMAKEVER set "CMAKEVER=%%V"
)
del "%TEMP%\swim_cmake.txt" >nul 2>&1
echo   [OK]   cmake !CMAKEVER!

set "SDKINC=%PFX86%\Windows Kits\10\Include"
set "SDKFOUND="
if exist "%SDKINC%" (
  for /d %%D in ("%SDKINC%\10.*") do set "SDKFOUND=%%~nxD"
)
if not defined SDKFOUND (
  echo   [FAIL] 没找到 Windows 10/11 SDK
  echo          用 VS Installer 勾选 "Windows 11 SDK"
  exit /b 1
)
echo   [OK]   Windows SDK %SDKFOUND%
exit /b 0


rem ======================================================================
rem  Step 2 -- Python 3.10
rem  Autodesk ships FBX Python SDK wheels for cp310 only -- a hard
rem  requirement, not a preference. Probe with the py launcher, not python:
rem  winget does not refresh PATH in the window that ran it.
rem ======================================================================
:step2_python310
echo.
echo --- [2/7] Python 3.10 ------------------------------------
py -3.10 -V >nul 2>&1
if not errorlevel 1 (
  call :py310_version
  echo   [SKIP] 已有 Python !PY310VER!
  exit /b 0
)
if "%MODE%"=="check" (
  echo   [FAIL] 没有 Python 3.10 [FBX SDK 只支持这个 ABI]
  exit /b 1
)
where winget >nul 2>&1
if errorlevel 1 (
  echo   [FAIL] 没有 Python 3.10, 且 winget 不可用
  echo          手工装: https://www.python.org/downloads/release/python-31011/
  exit /b 1
)
echo   正在用 winget 安装 Python 3.10 [几分钟]...
winget install --id Python.Python.3.10 --exact --silent --accept-package-agreements --accept-source-agreements
py -3.10 -V >nul 2>&1
if errorlevel 1 (
  echo   [FAIL] 安装完仍然找不到 py -3.10
  echo          关掉这个窗口重开一个再跑本脚本[PATH 需要刷新]
  exit /b 1
)
call :py310_version
echo   [OK]   Python !PY310VER! 安装完成
exit /b 0

:py310_version
py -3.10 -V > "%TEMP%\swim_py.txt" 2>&1
set "PY310VER="
for /f "usebackq tokens=2" %%V in ("%TEMP%\swim_py.txt") do (
  if not defined PY310VER set "PY310VER=%%V"
)
del "%TEMP%\swim_py.txt" >nul 2>&1
exit /b 0


rem ======================================================================
rem  Step 3 -- rebuild .venv on 3.10
rem  A 3.13 venv cannot install fbx. Rename it aside rather than deleting,
rem  in case it holds hand-installed packages. .venv is already gitignored.
rem ======================================================================
:step3_venv
echo.
echo --- [3/7] 虚拟环境 .venv ---------------------------------
set "VENVVER="
if exist "%VENV%\pyvenv.cfg" (
  for /f "usebackq tokens=1,2 delims== " %%A in ("%VENV%\pyvenv.cfg") do (
    if /I "%%A"=="version" set "VENVVER=%%B"
  )
)
echo !VENVVER! | findstr /R /C:"^3\.10\." >nul 2>&1
if not errorlevel 1 (
  if exist "%VPY%" (
    echo   [SKIP] .venv 已是 Python !VENVVER!
    exit /b 0
  )
)
if "%MODE%"=="check" (
  if not exist "%VPY%" (
    echo   [FAIL] .venv 不存在
  ) else (
    echo   [FAIL] .venv 是 Python !VENVVER!, 需要 3.10
  )
  exit /b 1
)
if exist "%VENV%\pyvenv.cfg" (
  for /f "usebackq tokens=*" %%T in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"`) do set "STAMP=%%T"
  echo   现有 .venv 是 Python !VENVVER!, 备份为 .venv.bak-!STAMP!
  move "%VENV%" "%ROOT%\.venv.bak-!STAMP!" >nul
  if errorlevel 1 (
    echo   [FAIL] 备份失败, 可能有进程正占用 .venv [关掉编辑器/终端再试]
    exit /b 1
  )
)
echo   正在用 Python 3.10 创建 .venv...
py -3.10 -m venv "%VENV%"
if errorlevel 1 (
  echo   [FAIL] venv 创建失败
  exit /b 1
)
echo   [OK]   .venv 已建立
exit /b 0


rem ======================================================================
rem  Step 4 -- Python deps (requirements-win.txt, plus pose file in pose mode)
rem ======================================================================
:step4_pydeps
echo.
echo --- [4/7] Python 依赖 ------------------------------------
if not exist "%VPY%" (
  echo   [FAIL] .venv 里没有 python.exe
  exit /b 1
)
"%VPY%" -c "import numpy, cv2" >nul 2>&1
set "COREOK=%ERRORLEVEL%"
if "%MODE%"=="check" (
  if "%COREOK%"=="0" (
    call :echo_pyver
    exit /b 0
  )
  echo   [FAIL] .venv 里 numpy/cv2 导入失败
  exit /b 1
)
if not "%COREOK%"=="0" (
  echo   正在升级 pip...
  "%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
  echo   正在安装 requirements-win.txt...
  "%VPY%" -m pip install -r "%ROOT%\requirements-win.txt" --disable-pip-version-check
  if errorlevel 1 (
    echo   [FAIL] 核心依赖安装失败
    exit /b 1
  )
) else (
  echo   [SKIP] numpy/cv2 已就绪
)
if "%MODE%"=="pose" (
  "%VPY%" -c "import torch, ultralytics" >nul 2>&1
  if errorlevel 1 (
    echo   正在安装 requirements-pose.txt [约 2.5GB, 慢]...
    "%VPY%" -m pip install -r "%ROOT%\requirements-pose.txt" --disable-pip-version-check
    if errorlevel 1 (
      echo   [FAIL] pose 依赖安装失败
      exit /b 1
    )
  ) else (
    echo   [SKIP] torch/ultralytics 已就绪
  )
)
call :echo_pyver
exit /b 0

rem Print the venv numpy/cv2 versions. Temp file, not for /f backquotes: the
rem nested quotes are eaten by cmd and the backquote form emits nothing.
:echo_pyver
"%VPY%" -c "import numpy,cv2;print('numpy',numpy.__version__,'cv2',cv2.__version__)" > "%TEMP%\swim_pyver.txt" 2>nul
for /f "usebackq delims=" %%V in ("%TEMP%\swim_pyver.txt") do echo   [OK]   %%V
del "%TEMP%\swim_pyver.txt" >nul 2>&1
exit /b 0


rem ======================================================================
rem  Step 5 -- Autodesk FBX Python SDK
rem  No PyPI package. The official installer is GUI and needs UAC (/S silent
rem  is refused with exit=5), so pop it up, then pip-install the wheel.
rem ======================================================================
:step5_fbxsdk
echo.
echo --- [5/7] FBX Python SDK ---------------------------------
"%VPY%" -c "import fbx" >nul 2>&1
if not errorlevel 1 (
  echo   [SKIP] import fbx 已可用
  exit /b 0
)
if "%MODE%"=="check" (
  echo   [FAIL] import fbx 失败 [水下 extract 步骤会跑不了]
  exit /b 1
)

call :find_fbx_wheel
if defined FBXWHEEL goto :fbx_pip

echo   本机没有 FBX SDK, 从 Autodesk 官网下载安装器...
if not exist "%DOWNLOADS%" mkdir "%DOWNLOADS%" >nul 2>&1
if exist "%FBX_EXE%" (
  echo   [SKIP] 安装包已在 %FBX_EXE%
) else (
  curl -L --fail --progress-bar -o "%FBX_EXE%" "%FBX_URL%"
  if errorlevel 1 (
    echo   [FAIL] 下载失败. 手工下载后放到 %FBX_EXE% 再重跑:
    echo          %FBX_URL%
    exit /b 1
  )
)
for %%F in ("%FBX_EXE%") do set "GOTBYTES=%%~zF"
if not "!GOTBYTES!"=="%FBX_BYTES%" (
  echo   [WARN] 安装包大小 !GOTBYTES! 与预期 %FBX_BYTES% 不一致
  echo          [Autodesk 可能更新了版本, 继续尝试安装]
)

echo.
echo   ******************************************************
echo   *  即将弹出 FBX Python SDK 安装界面                  *
echo   *  一路点"下一步", 保持默认安装路径即可              *
echo   *  装完关掉安装器, 本脚本会自动继续                  *
echo   ******************************************************
echo.
start /wait "" "%FBX_EXE%"

call :find_fbx_wheel
if not defined FBXWHEEL (
  echo   [FAIL] 安装后仍然找不到 .whl
  echo          手工做这两步:
  echo            1. 在 "%FBX_ROOT%" 下找 fbx-*-cp310-cp310-win_amd64.whl
  echo            2. "%VPY%" -m pip install 那个whl的完整路径
  exit /b 1
)

:fbx_pip
echo   找到轮子: !FBXWHEEL!
"%VPY%" -m pip install "!FBXWHEEL!" --disable-pip-version-check
if errorlevel 1 (
  echo   [FAIL] pip 安装 fbx 轮子失败
  exit /b 1
)
"%VPY%" -c "import fbx" >nul 2>&1
if errorlevel 1 (
  echo   [FAIL] 装完了但 import fbx 仍失败
  echo          轮子的 ABI 可能和 .venv 不匹配[需要 cp310]
  exit /b 1
)
echo   [OK]   import fbx 可用
exit /b 0

rem Find the cp310 wheel under the Autodesk install dir; result in FBXWHEEL.
:find_fbx_wheel
set "FBXWHEEL="
if not exist "%FBX_ROOT%" exit /b 0
for /f "usebackq delims=" %%W in (`dir /b /s "%FBX_ROOT%\*.whl" 2^>nul`) do (
  echo %%~nxW | findstr /I /C:"cp310" >nul 2>&1
  if not errorlevel 1 set "FBXWHEEL=%%W"
)
if not defined FBXWHEEL (
  for /f "usebackq delims=" %%W in (`dir /b /s "%FBX_ROOT%\*.whl" 2^>nul`) do set "FBXWHEEL=%%W"
)
exit /b 0


rem ======================================================================
rem  Step 6 -- third_party (FFmpeg+cuvid, GLFW) and the CUDA check
rem  CMake enables the cudagl backend only when all three are present;
rem  otherwise it silently degrades to d3d11 alone.
rem ======================================================================
:step6_thirdparty
echo.
echo --- [6/7] third_party 与 CUDA ----------------------------

if exist "%ROOT%\third_party\ffmpeg\include" (
  echo   [SKIP] third_party\ffmpeg 已就绪
) else (
  if "%MODE%"=="check" (
    echo   [FAIL] 缺 third_party\ffmpeg [cudagl 后端会被跳过]
    set "FAILED=1"
  ) else (
    call :fetch_zip "%FFMPEG_URL%" "ffmpeg.zip" "%ROOT%\third_party\ffmpeg" || exit /b 1
  )
)

if exist "%ROOT%\third_party\glfw\include" (
  echo   [SKIP] third_party\glfw 已就绪
) else (
  if "%MODE%"=="check" (
    echo   [FAIL] 缺 third_party\glfw [cudagl 后端会被跳过]
    set "FAILED=1"
  ) else (
    call :fetch_zip "%GLFW_URL%" "glfw.zip" "%ROOT%\third_party\glfw" || exit /b 1
  )
)

if exist "%CUDA_DIR%\include\cuda.h" (
  echo   [OK]   CUDA: %CUDA_DIR%
) else (
  echo   [WARN] 没找到 CUDA Toolkit [查过 %CUDA_DIR%]
  echo          cudagl 后端会被跳过, d3d11 后端不受影响.
  echo          需要的话装: winget install Nvidia.CUDA
  set "WARNED=1"
)
if "%MODE%"=="check" if defined FAILED exit /b 1
exit /b 0

rem Download and unpack a zip, stripping its single top-level directory.
rem   %1=url  %2=cache file name  %3=destination directory
:fetch_zip
set "FZ_URL=%~1"
set "FZ_NAME=%~2"
set "FZ_DEST=%~3"
set "FZ_ZIP=%DOWNLOADS%\%FZ_NAME%"
set "FZ_TMP=%DOWNLOADS%\x_%FZ_NAME%"

if not exist "%DOWNLOADS%" mkdir "%DOWNLOADS%" >nul 2>&1
if exist "%FZ_ZIP%" (
  echo   [SKIP] 压缩包已缓存: %FZ_NAME%
) else (
  echo   正在下载 %FZ_NAME% ...
  curl -L --fail --progress-bar -o "%FZ_ZIP%" "%FZ_URL%"
  if errorlevel 1 (
    echo   [FAIL] 下载失败: %FZ_URL%
    exit /b 1
  )
)

if exist "%FZ_TMP%" rmdir /s /q "%FZ_TMP%" >nul 2>&1
mkdir "%FZ_TMP%" >nul 2>&1
echo   正在解压 %FZ_NAME% ...
rem Expand-Archive, not the bundled tar: bsdtar rejects these zips with
rem "This does not look like a tar archive".
powershell -NoProfile -Command "Expand-Archive -LiteralPath '%FZ_ZIP%' -DestinationPath '%FZ_TMP%' -Force"
if errorlevel 1 (
  echo   [FAIL] 解压失败: %FZ_ZIP%
  exit /b 1
)

rem One top-level dir and no files means that dir is the real root (both the
set "FZ_INNER=%FZ_TMP%"
set /a FZ_DIRS=0
set /a FZ_FILES=0
for /d %%D in ("%FZ_TMP%\*") do (
  set /a FZ_DIRS+=1
  set "FZ_ONE=%%D"
)
for %%F in ("%FZ_TMP%\*") do set /a FZ_FILES+=1
if !FZ_DIRS! EQU 1 if !FZ_FILES! EQU 0 set "FZ_INNER=!FZ_ONE!"

if not exist "%FZ_DEST%" mkdir "%FZ_DEST%" >nul 2>&1
xcopy "!FZ_INNER!\*" "%FZ_DEST%\" /E /I /Y /Q >nul
if errorlevel 1 (
  echo   [FAIL] 拷贝到 %FZ_DEST% 失败
  exit /b 1
)
rmdir /s /q "%FZ_TMP%" >nul 2>&1
echo   [OK]   %FZ_DEST%
exit /b 0


rem ======================================================================
rem  Step 7 -- mesh, asset, build, runtime DLLs
rem ======================================================================
:step7_buildall
echo.
echo --- [7/7] 网格 / 资产 / 构建 -----------------------------

rem 7.1 pool_mesh.json is gitignored yet a hard CMakeLists.txt:26 dependency,
rem     so without it every target fails while building pool_4k.swasset.
if exist "%ROOT%\outputs\data\pool_mesh.json" (
  echo   [SKIP] outputs\data\pool_mesh.json 已存在
) else (
  if "%MODE%"=="check" (
    echo   [FAIL] 缺 outputs\data\pool_mesh.json [构建会失败在 pool_4k.swasset]
    exit /b 1
  )
  echo   正在从 inputs\pool\models\pool.fbx 提取 pool_mesh.json...
  "%VPY%" -m python.assets.extract_fbx
  if errorlevel 1 (
    echo   [FAIL] pool 网格提取失败
    exit /b 1
  )
  echo   [OK]   outputs\data\pool_mesh.json
)

rem 7.2 underwater 16-lane mesh + .swasset
if exist "%ROOT%\build\assets\generated\underwater_16.swasset" (
  echo   [SKIP] underwater_16.swasset 已存在
) else (
  if "%MODE%"=="check" (
    echo   [FAIL] 缺 build\assets\generated\underwater_16.swasset
    exit /b 1
  )
  if not exist "%ROOT%\inputs\underwater\models\all.fbx" (
    echo   [FAIL] 缺 inputs\underwater\models\all.fbx [本地重资产, 未进版本库]
    echo          从另一台机器拷 all.fbx 与 all.fbm\ 过来
    exit /b 1
  )
  echo   正在提取水下 16 块网格并编译 .swasset...
  "%VPY%" -m python.underwater.run --steps extract,asset --backend d3d11
  if errorlevel 1 (
    echo   [FAIL] 水下资产生成失败
    exit /b 1
  )
  echo   [OK]   build\assets\generated\underwater_16.swasset
)

rem 7.3 build swim_realtime
rem     One build tree per backend: python.underwater.run looks under
rem     build\win-<backend> (run.py: build_dir_for), so configure both or
rem     run_underwater.ps1 -Backend cudagl reconfigures and lacks its DLLs.
rem     Skip the cudagl tree when CUDA or third_party is missing.
set "BACKENDS=d3d11"
if exist "%CUDA_DIR%\include\cuda.h" (
  if exist "%ROOT%\third_party\ffmpeg\include" (
    if exist "%ROOT%\third_party\glfw\include" set "BACKENDS=d3d11 cudagl"
  )
)
for %%B in (%BACKENDS%) do (
  call :build_one %%B
  if errorlevel 1 exit /b 1
)
exit /b 0

:build_one
set "BK=%~1"
set "BKDIR=%ROOT%\build\win-%BK%"
set "BKEXE=%BKDIR%\Release\swim_realtime.exe"
if "%MODE%"=="check" (
  if exist "%BKEXE%" (
    echo   [OK]   %BK%: swim_realtime.exe
  ) else (
    echo   [FAIL] 缺 build\win-%BK%\Release\swim_realtime.exe
    exit /b 1
  )
) else (
  if exist "%BKEXE%" (
    echo   [SKIP] %BK%: swim_realtime.exe 已存在
  ) else (
    echo   正在配置并构建 %BK% [Release]...
    cmake -S "%ROOT%" -B "%BKDIR%" -G "Visual Studio 17 2022" -A x64 -DPython3_EXECUTABLE="%VPY%" >nul
    if errorlevel 1 (
      echo   [FAIL] %BK%: CMake 配置失败
      exit /b 1
    )
    cmake --build "%BKDIR%" --target swim_realtime --config Release >nul
    if errorlevel 1 (
      echo   [FAIL] %BK%: 构建失败[去掉本行的 ^>nul 可看完整报错]
      exit /b 1
    )
    echo   [OK]   %BK%: %BKEXE%
  )
)
rem Runtime DLLs: CMake never copies these; without them the exe dies 0xc0000135.
call :copy_runtime_dlls "%BKDIR%\Release" %BK%
exit /b 0

:copy_runtime_dlls
set "DEST=%~1"
set "DESTBK=%~2"
if not exist "%DEST%" (
  if "%MODE%"=="check" (
    echo   [FAIL] 输出目录不存在: %DEST%
    exit /b 1
  )
  exit /b 0
)
set "MISSINGDLL="
for %%D in (avcodec avformat avutil swresample swscale) do (
  if not exist "%DEST%\%%D-*.dll" (
    if exist "%ROOT%\third_party\ffmpeg\bin\%%D-*.dll" (
      if not "%MODE%"=="check" copy /Y "%ROOT%\third_party\ffmpeg\bin\%%D-*.dll" "%DEST%\" >nul
    ) else (
      set "MISSINGDLL=!MISSINGDLL! %%D"
    )
  )
)
if not exist "%DEST%\glfw3.dll" (
  if exist "%ROOT%\third_party\glfw\lib-vc2022\glfw3.dll" (
    if not "%MODE%"=="check" copy /Y "%ROOT%\third_party\glfw\lib-vc2022\glfw3.dll" "%DEST%\" >nul
  ) else (
    set "MISSINGDLL=!MISSINGDLL! glfw3"
  )
)
if not exist "%DEST%\cudart64_12.dll" (
  if exist "%CUDA_DIR%\bin\cudart64_12.dll" (
    if not "%MODE%"=="check" copy /Y "%CUDA_DIR%\bin\cudart64_12.dll" "%DEST%\" >nul
  ) else (
    set "MISSINGDLL=!MISSINGDLL! cudart64_12"
  )
)
if defined MISSINGDLL (
  echo   [WARN] %DESTBK%: 找不到这些运行期 DLL:!MISSINGDLL!
  echo          d3d11 后端不依赖它们, cudagl 后端会起不来.
  set "WARNED=1"
) else (
  echo   [OK]   %DESTBK%: 运行期 DLL 已就位[ffmpeg/glfw/cudart]
)
exit /b 0


rem ======================================================================
:fatal
echo.
echo ==========================================================
echo   安装未完成 -- 上面第一个 [FAIL] 就是原因
echo ==========================================================
endlocal & exit /b 1

:summary
echo.
echo ==========================================================
if "%MODE%"=="check" (
  echo   体检通过, 环境完整
) else (
  echo   [OK] 环境就绪
)
if defined WARNED echo   [注意] 有 WARN 项, cudagl 后端可能不可用
echo.
echo   水下 16 路:  pwsh scripts\run_underwater.ps1 ^<采样目录^> -Seconds 30
echo   六路 4K:     scripts\run_win.bat
echo   自检:        scripts\install.bat check
echo ==========================================================
endlocal & exit /b 0
