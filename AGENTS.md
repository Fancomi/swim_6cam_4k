# Repository Guidelines

## 项目结构与模块组织

本仓库把泳池 FBX 的平面网格与 UV 编译成 GPU 运行时资产，再用三个原生后端做实时拼接。实时核心是可移植 C++20，位于 `cpp/core/`（`swim_core`）；后端在 `cpp/backends/` 下按平台隔离：`metal/`（macOS，VideoToolbox + Metal）、`d3d11/`（Windows，Media Foundation + Direct3D 11）、`cudagl/`（Windows，NVDEC + OpenGL）。三者通过 `cpp/core/include/swim/core/backend.hpp` 的 `IBackend`/`ISource`/`IRenderer` 契约接入，由 `cpp/app/main.cpp` 显式调 `register_<backend>_backend()`（各自 `std::call_once`）注册进 `BackendRegistry`，运行时按 config 的 `backend=` 名字取。刻意不做静态初始化自注册：链接顺序决定的注册时机是最难查的一类 bug。不允许后端专有类型泄漏进 `cpp/core/`。

仓库有**四条互不交叉的链路**，一个脚本一条，改哪条只看那一条：

| 链路 | 入口 | 代码 |
| --- | --- | --- |
| 相机拼接（pool / pool2 / underwater / underwater2 / overhead 五条相机线） | `scripts/run_stitch.{sh,ps1}` | `python/stitch/` + `cpp/` |
| 入水检测机位 | `scripts/run_water_entry.sh` | `python/water_entry/` |
| 数据集标注 | `scripts/run_label.sh` | `python/labeling/`、`python/keypoints/` |
| 性能取证 | `scripts/run_bench.sh` | `python/benchmarks/` |

`scripts\run_win.bat` 是 Windows 双击入口，走第一条链路——它与 `install.bat` 是两个例外：双击就要干活，所以不带参数即执行，用法写在顶部被 `goto` 跳过的说明区里。三个 `.sh` 入口不带参数或 `--help` 打印自己的用法，且说明只存在脚本顶部一处（用法函数把那段注释打出来，不再复制）；`run_bench.sh` 的选项清单是个例外，它带默认值太多，另用一段 heredoc。另有两个辅助入口 `run_fbx_overlay.sh`（入水机位网格叠加 + 米数）与 `check_inputs.sh`（标定数据验收），同样把用法写在顶部注释里。新增脚本请延续「一条链路一个入口」的口径，不要再按平台或语言拆。

Python 侧一个包一件事：`common/` 是跨链路共用的路径 / 图像 IO / CSV / HTML 页面骨架，`fbx_tools/` 是**唯一** import `fbx` 的地方，`dataset/` 看管不在 git 里的 `inputs/`，其余包对应上表四条链路——`labeling` 与 `keypoints` 同属标注链路。`tests/python/test_layout.py` 把这些约束写成了断言——包清单、每个包必须有 docstring、`import fbx` 只允许出现在 `fbx_tools/`、除 `common/paths.py` 外任何模块都不许自己算仓库根、**链路之间的 import 必须登记在 `CROSS_CHAIN_IMPORTS` 里**（目前四条，新增一条得先改断言）。测试在 `tests/`（`cpp/`、`python/`、`fixtures/`），运行时 config 在 `inputs/configs/`，`build/` 与 `outputs/` 是本机产物，不放手写源码或文档。

六条相机线共用一套拼接代码，**差异全部是 `python/stitch/profiles.py` 里的数据**（模型、相机 id、网格排序、每米像素、融合方式、时间对齐策略、参考贴图来源、泳道示意图、是否标米数）。加一条线应当只加一条 profile 记录；如果它需要一个新字段，那个字段本身就是需要先想清楚的东西。不要在 `python/stitch/` 的其他模块里按线路名分支。三个物理机位各有两条线：同一批相机换一份重建的 FBX 就是一条新线（`pool`/`pool2`、`underwater`/`underwater2`、`overhead`/`overhead2`），产物各自落 `outputs/<line>/`，两条线才能逐帧对比。

相机数量、相机 ID、输出尺寸都是**数据而非代码**：`kMaxCameras = 16`（`cpp/core/include/swim/core/camera_capacity.hpp`），config 里 `source.<id>=<path>` 的声明顺序即通道顺序。新增机位布局应通过 config + `.swasset` 表达，不要在 C++ 里加分支。

## 构建、测试与开发命令

Windows 一键装环境（C++ 与 Python 两侧）：

```bat
scripts\install.bat            :: 核心环境
scripts\install.bat pose       :: 追加 torch/ultralytics（约 2.5GB，仅入水检测）
scripts\install.bat check      :: 只体检，不改动任何东西
```

手工构建与运行：

```powershell
cmake -S . -B build/win-d3d11 -G "Visual Studio 17 2022" -A x64 -DPython3_EXECUTABLE=.venv\Scripts\python.exe
cmake --build build/win-d3d11 --config Release --target swim_realtime

pwsh scripts/run_stitch.ps1 pool extract,asset,build,live --backend cudagl
scripts\run_win.bat under <采样目录>                     # 水下 16 路
```

每个后端一棵独立构建树 `build/win-<backend>`（`python/stitch/run.py` 的 `build_dir_for` 就是这么找 exe 的）。CMake **不会**把 FFmpeg/GLFW/cudart 的 DLL 拷到 exe 旁，缺了会以 `0xC0000135` 启动失败；`install.bat` 与 `run.py` 的 `build` 步骤都会补这一步，新增构建路径时别忘了。

Python 3.10 是硬要求，不是偏好：Autodesk 只为 cp310 发布 FBX Python SDK 轮子，而 `python/fbx_tools/scene.py` 是模块级 `import fbx`。反过来说，只读已提取的 mesh JSON 的代码必须在没有 `fbx` 的机器上也能跑——这就是把 SDK 关在一个包里的原因。

## 编码风格与命名约定

C++20，命名空间按层次分 `swim::core` / `swim::d3d11` / `swim::cudagl`。类型用大写驼峰（`RunLifecycle`、`LatestFrameMailbox`），函数与变量用小写下划线（`classify_eof`、`past_start_offset`），成员变量带尾下划线（`lifecycle_`、`loop_sources_`）。头文件放 `include/swim/<layer>/`，实现放 `src/`。Python 用 4 空格、小写下划线，模块入口一律 `python -m python.<pkg>[.<mod>]`。

C++ 与 Python 源码保持 UTF-8。注释解释**为什么**，尤其是那些看起来可以简化但实际不能的地方——仓库里已有多处这类注释（`view` 必须是 `Surface` 的第一个成员、`GL_UNPACK_ALIGNMENT` 必须置 1 等），删掉它们会让人重新踩坑。

## Windows bat 脚本编码规范

`scripts/**/*.bat` 必须使用 **UTF-8 无 BOM、CRLF 换行**，并允许保留中文说明和中文提示。不要把中文删掉来规避编码问题，也不要改成 GBK。

三条约束各有原因，缺一不可：

- **无 BOM**：`cmd.exe` 会把 UTF-8 BOM 当成第一条命令的一部分，`@echo off` 变成 `锘緻echo off` 这类乱码命令。
- **`chcp 65001 >nul`**：把当前批处理进程切到 UTF-8 代码页，否则中文 `echo` 输出乱码。
- **中文只放在 `goto :run` 跳过的说明区**：`:run` 之后的注释一律 ASCII。`cmd.exe` 对多字节 `rem` 行的解析是位置相关的——同一行中文注释可能今天正常，明天因为上游插了一行就变成 `'开一个正常显示的调试窗口，' is not recognized as an internal or external command`。实测确认过：把同样的中文从执行区移进 `goto` 跳过区，问题消失。

`echo` 输出的中文不受此限制，`chcp 65001` 之后能正常显示；受限的只是执行区里的**注释**。

脚本骨架：第一行 ASCII 的 `@echo off`，第二行 `chcp 65001 >nul`，第三行 `goto :run`，中文说明放在它和 `:run` 标签之间，真正执行的命令从 `:run` 之后开始。

```bat
@echo off
chcp 65001 >nul
goto :run

:说明
REM 中文说明放这里，这个区域会被 goto 跳过，永远不会被当命令执行。
REM 用法、参数、坑位注释都写在这一段。

:run
setlocal EnableDelayedExpansion
cd /d "%~dp0.."

rem ASCII-only comments from here on.
set "EXE=%~dp0..\build\win-d3d11\Release\swim_realtime.exe"
"%EXE%" --config "inputs\configs\windows_20260629.conf"
```

路径变量必须用 `set "KEY=value"`，调用路径必须加引号，避免路径含空格时失败。修复同类问题从文件编码和脚本格式入手，不要求用户改编辑器、PowerShell 或 cmd 的全局配置。

仓库通过 `.gitattributes` 强制 `*.bat` 用 CRLF，通过 `.editorconfig` 约束 UTF-8 无 BOM + CRLF。改动或新增 `scripts/**/*.bat` 后必须运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\checks\check_bat_format.ps1
```

检查失败时修编码、换行或前三行格式，不要删中文说明。

### cmd.exe 的其他已知陷阱

这些都是实测踩过的，写下来免得重复调试：

- `for /f` 反引号形式在命令行含 `)` 或嵌套引号时会**静默什么都不输出**（例如 vswhere 的 `-version "[17.0,18.0)"`）。改成重定向到临时文件再 `for /f` 读它。
- 自带的 `tar`（bsdtar）解不开某些 GitHub release zip，报 `This does not look like a tar archive`。用 `powershell -Command "Expand-Archive ..."`。
- `%ProgramFiles(x86)%` 里的括号在 `if`/`for` 的括号块里直接展开会截断语法，先落到普通变量再用。

## 测试指南

C++ 测试是 `swim_core_tests`（`tests/cpp/`，自建轻量 harness，`PASS`/`FAIL` 逐行输出）：

```powershell
cmake --build build/win-d3d11 --config Release --target swim_core_tests
.\build\win-d3d11\Release\swim_core_tests.exe
```

Python 测试用 unittest：`.venv\Scripts\python.exe -m unittest discover -s tests/python -t .`

新增 config 键时，`tests/cpp/test_config.cpp` 的文件加载与 CLI override 两条路径都要加断言，`tests/fixtures/cpp/valid.conf` 也要同步。

改动实时链路后跑真实数据冒烟，并记录命令与关键指标。判定标准不只看帧率，还要看 `decoded_pixel_host_copies=0`（没把解码像素读回 CPU）、`pool_exhaustion=0`、`malformed=0`。测试代码要保持跨平台：`tests/cpp/test_config.cpp` 曾因用了 POSIX 的 `setenv` 而在 MSVC 上编不过。

改动拼接几何或资产编译时，验收标准是**逐字节**：五条线的 `.swasset` 与静图必须与改动前完全一致，除非改的正是那个口径。哪一条线该有多大画布、每米多少像素，README 的对照表里写着；不确定就先编一份、比 sha256，再动手。

不需要再补设计文档或实施计划：口径写在 profile 记录与代码注释里，取证写在 README 的实测表里。

## 提交与 Pull Request 规范

提交信息用 `type(scope): 简短祈使句`，与现有历史一致：`feat(underwater): align the realtime stitch with the offline mp4 pipeline`、`fix(build): depend on the mesh JSON by absolute path`。PR 说明改动目标、影响的 target、构建命令、手工验证结果与实测指标。

## 安全与配置提示

不要提交大型视频、`.swasset`、`third_party/` 预编译依赖、`build/` 或 `outputs/` 产物。

**`inputs/` 的标定数据整体不在 git 里**（除 `configs/`）：225MB 的两代 FBX 与实拍贴图，已于 2026-08-17 从全部历史抹除，也没有走 LFS——GitHub 免费额度 1GiB 存储 + 1GiB/月流量装不下按 FBX 版本迭代的它。数据带外搬运，两代差异与验收指南见 `docs/DATA.md`，搬完跑 `./scripts/check_inputs.sh [v1|v2]`；清单 `docs/data-manifest.tsv` 的路径由 profiles 导出而非手写（`python/dataset/`）。`.gitignore` 的写法是 `inputs/*` 全忽略再放行 `configs/` 里两组手写 config，**不要再往 inputs 加 `!` 例外**。

其余被 gitignore 但运行必需的，新机器靠 `install.bat` 补齐：`outputs/pool/mesh.json`（`CMakeLists.txt` 的硬依赖，缺了任何 target 都编不过；用 `run_stitch.sh pool extract` 从 `pool.fbx` 生成，所以**只想编 C++ 也得先有 pool 的一代数据**）、`third_party/{ffmpeg,glfw}`、`.venv`。

`inputs/configs/<line>_<backend>.conf` 是 `python.stitch` 每次按片段目录重新生成的，不要手工维护；手写的参考 config 只有 `macos_*.conf` 与 `windows_*.conf`。
