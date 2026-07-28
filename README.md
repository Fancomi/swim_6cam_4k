# Swim FBX Demo

## 项目简介

本项目把泳池 FBX 中的平面网格与 UV 映射提取为 JSON，再使用六路相机的合成纹理或视频帧生成俯视泳池拼接图像和 H.264 视频。FBX SDK 只参与 UV 烘焙和网格提取；常规渲染由 NumPy、OpenCV 和 FFmpeg 完成。

本文所有命令都假定当前目录是项目根目录 `swim_fbx_demo/`。

## Windows 实时路径（Direct3D 11 + Media Foundation）

仓库也包含与 Metal 平级的 Windows 原生后端 `cpp/backends/d3d11/`：六路 H.264 由 Media Foundation（`IMFSourceReader` + `IMFDXGIDeviceManager`）做 D3D11 硬件解码为 GPU 常驻 NV12 纹理，Direct3D 11 以同一套固定六网格 + FP16 加性累加 shader（`cpp/backends/d3d11/shaders/stitch.hlsl`，从 `stitch.metal` 逐字段移植）合成 `5002x2102`，再经 DXGI 交换链窗口实时预览。与 Metal 路径一致：不依赖 OpenCV/FFmpeg，不把解码像素读回 CPU，各路输入走容量有界的 latest-frame 交换。第一阶段覆盖 `解码 → GPU 拼接 → 预览` 端到端实时；硬件 HEVC 编码与 benchmark 矩阵为后续阶段。

构建前置：需要一个装有 `numpy` 与 `opencv-python` 的 Python 3.10+（用于把 `outputs/data/pool_mesh.json` 编译成 `assets/generated/pool_4k.swasset`），以及 Visual Studio 2022（MSVC，C++20）和 Windows 10 SDK。统一入口是 `scripts/run_win.ps1`：

```powershell
# 可视化 demo：DXGI 预览窗口 + 六路实时拼接（默认 30 秒）
pwsh scripts/run_win.ps1 demo

# 无窗口（仍执行真实 GPU 拼接与 present 计量）
pwsh scripts/run_win.ps1 demo -NoWindow
```

该脚本在缺少 `.swasset` 时自动编译，随后用 `-G "Visual Studio 17 2022"` 配置并构建 Release，再运行 `swim_realtime --backend d3d11`。六路真实输入路径写在 `configs/windows_20260629.conf`。

## Windows 实时路径（CUDA/GL：NVDEC + OpenGL）

另有一个与 D3D11 平级的 Windows 后端 `cpp/backends/cudagl/`，对齐 `rtsp-h264-stitcher` 的 NVDEC/CUDA/GL 技术栈，为后续接 RTSP 网络流与 NVENC 推流铺路：六路 H.264 由 FFmpeg 的 `h264_cuvid`（NVDEC）解码，帧直接落在 CUDA 设备内存（`AV_PIX_FMT_CUDA` NV12，无 host copy）；`cuGraphicsGLRegisterImage` + `cuMemcpy2D` 把 NV12 双平面上传到 CUDA 注册的 GL 纹理，OpenGL 3.3 用同一套六网格 + 羽化 GLSL（从 `stitch.metal` 移植）做 FP16 加性累加与归一化，GLFW 窗口呈现。GL 函数通过 `glfwGetProcAddress` 手动加载，不依赖 GLEW。

依赖（预编译，放在 `third_party/`，已 gitignore）：BtbN 的 FFmpeg shared 构建（含 cuvid/nvenc）、GLFW 3.4 win64，以及本机 CUDA Toolkit（头文件与 `cuda.lib`/`cudart.lib`）。CMake 通过 `SWIM_FFMPEG_DIR` / `SWIM_GLFW_DIR` / `SWIM_CUDA_DIR` 定位；三者齐备时自动启用 `swim_cudagl_backend`（配置日志打印 `CUDA/GL backend: enabled`）。

两个后端共用同一个入口脚本 `scripts\run_win.bat`，第一个参数选后端；运行时 stderr 每秒刷新一行 render / decode / preview 实时 FPS：

```bat
scripts\run_win.bat                     :: d3d11 后端（默认），预览窗口，30 秒
scripts\run_win.bat cudagl              :: CUDA/GL（NVDEC+OpenGL）后端
scripts\run_win.bat cudagl 60           :: 跑 60 秒
scripts\run_win.bat cudagl 30 nowindow  :: 无窗口（仍执行真实 GPU 拼接）
scripts\run_win.bat cudagl fps:60       :: 指定渲染帧率 60fps（与输入帧率无关）
```

渲染帧率可用 `fps:N` 指定（底层 `--fps=N`，等价 `fps_num=N fps_den=1`），与输入视频帧率无关：latest-frame 邮箱按该节奏重复或丢弃源帧来满足目标 cadence。也可在 config 用 `fps_num`/`fps_den` 或 CLI `--fps-num`/`--fps-den` 设非整数帧率（如 30000/1001）。

CUDA/GL 的六路真实输入路径写在 `configs/windows_cudagl.conf`（`backend=cudagl`）。运行时需要 FFmpeg（`avcodec/avformat/avutil/swresample/swscale`）、`glfw3.dll`、`cudart64_12.dll` 与 exe 同目录。

## 实时 Metal 路径

仓库同时包含独立的 macOS 实时实现：六路 H.264 由 VideoToolbox 解码为 GPU 可见表面，Metal 以固定六网格合成 `5002x2102` 输出，并可分流到 preview 与硬件 HEVC。该运行路径不依赖 OpenCV 或 FFmpeg，不把解码像素读回 CPU；各路输入采用容量有界的 latest-frame 交换，接收端只消费当下最新完整帧。

代码按语言隔离：实时核心位于 `cpp/core/`，Apple 原生后端位于 `cpp/backends/metal/`，Windows 原生后端位于 `cpp/backends/d3d11/`，离线资产与验证工具位于 `python/`，运行入口位于 `scripts/`。默认现场数据集仍是：

```text
/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K
```

统一入口是 `scripts/run_metal.sh`：

```bash
# 可视化 demo：AppKit 窗口 + 写出 HEVC（默认 30 秒）
./scripts/run_metal.sh demo

# 无窗口、不写文件（只跑 GPU preview/encode 空 sink）
./scripts/run_metal.sh demo --no-window --no-encode

# 性能矩阵 / soak
./scripts/run_metal.sh benchmarks --quick
./scripts/run_metal.sh benchmarks --duration 15
./scripts/run_metal.sh soak
```

`demo` 默认 `--preview-visible=true`，会创建 AppKit/CAMetalLayer 窗口显示实时拼接画面，并把硬件 HEVC 写到 `outputs/videos/pool_metal.h265`，指标写到 `outputs/benchmarks/manual.jsonl`。`--no-window` 不是跳过 preview：它仍创建私有 Metal render target，对每个被接受的最新输出执行真实 shader copy/render command，并等待 GPU completion。benchmarks/soak 默认无窗口；需要窗口时加 `--visible`。

## Release 性能矩阵

完整矩阵覆盖六个真实 stage、`1/2/4/6` 路输入以及 paced/unpaced 两种节奏，共 48 个 cell。脚本只构建一次 Release、只计算一次 asset/六源 SHA-256，每个 cell 写独立 JSONL 并立即校验；任一进程或校验失败都会停止，不会静默重试或拼入最终结果。

一秒功能矩阵由主测试流程执行：

```bash
./scripts/run_metal.sh benchmarks --quick
```

可发布矩阵每个 cell 至少 15 秒：

```bash
./scripts/run_metal.sh benchmarks --duration 15
```

结果位于 `outputs/benchmarks/runs/<run_id>/`，成功后 `outputs/benchmarks/latest` 指向该目录：

- `cells/*.jsonl`：带唯一 `(stage, stream_count, pacing)` 身份的原始 cell；
- `results.jsonl`：48 个 cell 全部通过后才生成的合并记录；
- `summary.csv`、`summary.md`：最终吞吐、区间 p50/p95、瓶颈排名及 preview/encode 增量成本；
- `manifest.json`：run/build/hash 身份与 `publishable` 标志；不足 15 秒始终为 `false`。

可单独复验已生成的矩阵：

```bash
.venv/bin/python -m python.validation.summarize_benchmarks \
  outputs/benchmarks/latest/results.jsonl
```

默认十分钟的六路 paced full soak：

```bash
./scripts/run_metal.sh soak
```

soak 按每条 interval 的真实 `elapsed_s` 累加时间轴，报告 RSS 与 Metal allocation 的每分钟线性斜率，并拒绝 host copy、容量 high-water 越界、编码 callback/drain 错误，以及 warm-up 后连续五个区间低于 29 FPS。默认 RSS 增长上限为 64 MiB/min，Metal allocation 上限为 32 MiB/min；可用 `--max-rss-slope` 和 `--max-gpu-slope` 显式覆盖。

## 处理流程

1. `./scripts/run_python.sh bake ...` 可选地把中线 UV 延伸写入一个新的 FBX，原始模型不需要被覆盖。
2. `./scripts/run_python.sh extract` 读取 `inputs/pool/models/pool.fbx`，提取三角形、二维位置和 UV，默认写入 `outputs/data/pool_mesh.json`。
3. `./scripts/run_python.sh still` / `4k` 使用网格 JSON 与纹理或六路视频生成静态拼接图或 H.264 视频。
4. `./scripts/run_python.sh asset` 把网格 JSON 编译为 Metal 运行时 `.swasset`。
5. `./scripts/run_python.sh keypoint` 生成 2D 关键点裁剪复核页。
6. `./scripts/run_python.sh oh-merge` 把 `overhead5` / `overhead6` / `orbbec_camera_1` 各自全时段快照合成为一张原始分辨率的 UV 参考图（另附中值背景帧），输出到 `outputs/annotation_preview/overhead-merge/`。
7. `./scripts/run_python.sh label mask` 起本地服务打开保留区域 mask 标注器：选一台相机、逐帧翻、拖拽画胶囊笔画标出该帧要保留的区域，存为数据集根目录下的 `mask_label_project.json`。`label dot` 打开打点标注器。加 `--selftest` 打开该标注器的浏览器自测页。

两个标注器都用 ES module，`file://` 下会被浏览器按 CORS 拦截（origin 为 `null`）导致白屏，所以必须经 `label` 子命令走 http 打开，不要双击 html。

## 目录结构

```text
swim_fbx_demo/
├── README.md
├── .venv/                         # 现有 macOS / Python 3.10 虚拟环境
├── inputs/
│   ├── models/
│   │   └── pool.fbx
│   └── textures/
│       ├── camera_1_composite.png
│       ├── camera_2_composite.png
│       ├── camera_3_composite.png
│       ├── camera_4_composite.png
│       ├── camera_5_composite.png
│       └── camera_6_composite.png
├── outputs/
│   ├── data/
│   │   └── pool_mesh.json
│   ├── images/
│   │   ├── pool.png
│   │   ├── pool_grid.png
│   │   └── pool_grid_preview.png
│   ├── videos/
│   │   ├── pool.mp4
│   │   ├── pool_.mp4
│   │   ├── pool_4k_test10s.mp4
│   │   └── pool_4k_full.mp4
│   └── logs/
│       └── pool_4k_full.log
├── python/
│   ├── __init__.py
│   ├── assets/
│   │   ├── __init__.py
│   │   ├── bake_uv.py
│   │   ├── build_keypoint_preview.py
│   │   ├── extract_fbx.py
│   │   ├── fbx_common.py
│   │   └── keypoint_preview.py
│   ├── validation/
│   │   ├── __init__.py
│   │   └── reference_renderer.py
│   ├── water_entry/               # 入水检测机位：YOLO-pose 预测、复核与选帧
│   │   ├── __init__.py
│   │   ├── annotate_preview.py
│   │   ├── common.py
│   │   ├── export_package.py
│   │   ├── predict.py
│   │   ├── review.py
│   │   └── select_frames.py
│   └── tests/
│       ├── __init__.py
│       ├── test_keypoint_preview.py
│       └── test_layout.py
├── scripts/
│   ├── run_metal.sh              # demo / benchmarks / soak
│   ├── run_python.sh             # still / 4k / keypoint / extract / bake / asset / uw-* / we-*
│   ├── run_underwater.sh         # 水下 16 路实时拼接一键（macOS / Linux）
│   ├── run_underwater.ps1        # 同上（Windows）
│   ├── run_win.ps1               # Windows 六路实时启动器
│   ├── run_win.bat               # 同上（cmd 包装）
│   └── run_water_entry.sh        # 入水检测机位难例筛选全流程
└── docs/
    └── superpowers/
        ├── plans/
        │   └── 2026-07-10-project-layout.md
        └── specs/
            └── 2026-07-10-project-layout-design.md
```

`inputs/` 保存项目自带的源模型和合成纹理；`outputs/data/` 保存由 FBX 派生的网格数据；`outputs/images/`、`outputs/videos/` 和 `outputs/logs/` 保存渲染产物与日志。外部 4K 原始视频不复制进项目。

## 环境依赖

- Python 3.10
- Autodesk FBX Python SDK，Python 中需能 `import fbx`
- NumPy
- OpenCV，Python 包导入名为 `cv2`
- FFmpeg，`ffmpeg` 可执行文件需位于 `PATH`
- 仅入水检测机位（`python/water_entry/`）需要：PyTorch `2.5.1`、TorchVision `0.20.1`、Ultralytics（`8.4.x`，MPS 后端在 Apple Silicon 上可用）

项目现有 `.venv/` 是面向 macOS 和 Python 3.10 的环境，不应直接复制到其他操作系统或其他 Python 版本使用。可先检查当前环境：

```bash
.venv/bin/python --version
.venv/bin/python -c "import fbx, numpy, cv2; print('Python dependencies OK')"
.venv/bin/python -c "import torch, ultralytics; print('pose deps OK', torch.backends.mps.is_available())"
ffmpeg -version
```

Autodesk FBX Python SDK 的可用发行包和安装方式取决于操作系统、CPU 架构及 Python ABI；本项目不提供未经验证的通用安装命令。若不使用现有 `.venv/`，请先在目标 Python 3.10 环境中确认上述导入，再运行 FBX 相关脚本。

三个 Python 入口的实际参数可用以下命令查看：

```bash
.venv/bin/python -m python.assets.bake_uv --help
.venv/bin/python -m python.assets.extract_fbx --help
.venv/bin/python -m python.validation.reference_renderer --help
```

## 快速开始

项目已经包含网格 JSON 和合成纹理。无需重新读取 FBX 即可生成静态拼接图：

```bash
./scripts/run_python.sh still
```

默认外部 4K 数据集目录是：

```text
/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K
```

该目录应包含会话 `20260629_172532` 的六个 `camN.mp4` 文件。默认生成 10 秒测试片：

```bash
./scripts/run_python.sh 4k
```

## 重新提取 FBX 网格

从默认模型重新生成默认网格 JSON：

```bash
./scripts/run_python.sh extract
```

等价的显式调用如下；它会重写 `outputs/data/pool_mesh.json`：

```bash
.venv/bin/python -m python.assets.extract_fbx \
  inputs/pool/models/pool.fbx \
  outputs/data/pool_mesh.json \
  --tex-dir inputs/pool/textures
```

如需把中线 UV 延伸先烘焙到一个新模型，再提取该模型，可运行：

```bash
.venv/bin/python -m python.assets.bake_uv \
  inputs/pool/models/pool.fbx \
  outputs/data/pool_uv_baked.fbx \
  --ext-px 5 \
  --tex-dir inputs/pool/textures

.venv/bin/python -m python.assets.extract_fbx \
  outputs/data/pool_uv_baked.fbx \
  outputs/data/pool_mesh.json \
  --tex-dir inputs/pool/textures
```

第一条命令保留 `inputs/pool/models/pool.fbx`，并在已有的 `outputs/data/` 目录创建新 FBX；第二条命令更新渲染器默认使用的网格 JSON。

## 生成静态图和网格预览

生成完整分辨率的静态拼接图和网格叠加图：

```bash
.venv/bin/python -m python.validation.reference_renderer \
  --data outputs/data/pool_mesh.json \
  --tex-dir inputs/pool/textures \
  --still outputs/images/pool.png \
  --grid-still outputs/images/pool_grid.png
```

降低每米像素数可更快生成网格预览：

```bash
.venv/bin/python -m python.validation.reference_renderer \
  --data outputs/data/pool_mesh.json \
  --tex-dir inputs/pool/textures \
  --ppm 28 \
  --grid-still outputs/images/pool_grid_preview.png
```

`--ppm` 控制输出平面每米对应的像素数，默认值为 `100`。输出文件的父目录会自动创建。

## 渲染 4K 测试片和全长视频

显式指定 10 秒测试片及输出路径：

```bash
./scripts/run_python.sh 4k 10 outputs/videos/pool_4k_test10s.mp4
```

用 `SWIMMING_DATASET_DIR` 覆盖默认数据集目录：

```bash
SWIMMING_DATASET_DIR="/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K" \
  ./scripts/run_python.sh 4k 10 outputs/videos/pool_4k_test10s.mp4
```

请求覆盖整段约 602 秒的素材并写入全长输出：

```bash
./scripts/run_python.sh 4k 602 outputs/videos/pool_4k_full.mp4
```

渲染器在任一路输入先结束时停止，因此最终时长由最短输入决定；当前历史全长输出约为 601.87 秒。4K 全长渲染计算量较大，运行前应确认磁盘空间和可接受的耗时。

## 输入顺序与输出说明

视频通过位置与网格一一配对，顺序固定如下，不可按文件名自然排序后直接传入：

| 位置 | 网格节点 | 合成纹理 | 4K 视频 |
| ---: | --- | --- | --- |
| 1 | `01` | `inputs/pool/textures/camera_3_composite.png` | `20260629_172532_cam3.mp4` |
| 2 | `02` | `inputs/pool/textures/camera_2_composite.png` | `20260629_172532_cam2.mp4` |
| 3 | `03` | `inputs/pool/textures/camera_1_composite.png` | `20260629_172532_cam1.mp4` |
| 4 | `u` | `inputs/pool/textures/camera_4_composite.png` | `20260629_172532_cam4.mp4` |
| 5 | `Plane004` | `inputs/pool/textures/camera_5_composite.png` | `20260629_172532_cam5.mp4` |
| 6 | `Plane007` | `inputs/pool/textures/camera_6_composite.png` | `20260629_172532_cam6.mp4` |

也就是固定相机顺序：`cam3 cam2 cam1 cam4 cam5 cam6`。`scripts/run_python.sh 4k` 已按此顺序组装参数；直接调用 `python -m python.validation.reference_renderer --videos` 时也必须保持相同顺序。

主要输出如下：

- `outputs/data/pool_mesh.json`：FBX 派生的六块网格、三角形和 UV 数据，也是渲染器的默认输入。
- `outputs/images/pool.png`：静态拼接图。
- `outputs/images/pool_grid.png` 和 `outputs/images/pool_grid_preview.png`：网格及分区边界叠加图。
- `outputs/videos/pool_4k_test10s.mp4`：默认 4K 测试片。
- `outputs/videos/pool_4k_full.mp4`：历史全长拼接视频。
- `outputs/videos/pool_metal.h265`：实时 Metal demo 默认 HEVC 产物。
- `outputs/logs/pool_4k_full.log`：历史全长渲染日志；脚本本身不会自动把标准输出重定向到该文件。
- `outputs/keypoint_preview/`：关键点裁剪复核页（`index.html`、`crops/`、`report.json`）。

## 检查 2D 关键点裁剪标注

`python.assets.keypoint_preview` 从外部标注数据集解析 COCO-17 关键点，按人物裁剪出正方形预览图并叠加骨架、关键点和精准关键点框。脚本入口：

```bash
./scripts/run_python.sh keypoint
```

生成结果写入 `outputs/keypoint_preview/`，直接在浏览器打开 `outputs/keypoint_preview/index.html` 即可查看，无需额外的静态服务器。页面在桌面宽度下用四列网格展示裁剪卡片，每张卡片下方显示 `图 x/54 · 人 y/554` 形式的图片与人物计数元数据；卡片图片使用 `loading="lazy"` 和 `IntersectionObserver` 懒加载,只在滚动到附近时才请求对应裁剪图。图上红框（红框：精准关键点框）标出该人物可见关键点的精确外接框，黄色骨架线和关键点是叠加的 COCO-17 标注,红框之外的留白来自按 `--padding-ratio` 和 `--minimum-side` 计算的正方形裁剪范围。

可覆盖的参数：

- `--dataset-root PATH`：外部标注数据集根目录，默认是本机路径 `/Users/penghaotian/Downloads/DATAS/SWIMMING/游泳6拼接1080P-2D关键点标注`；
- `--output-dir PATH`：预览页与裁剪图的输出目录，默认 `outputs/keypoint_preview/`；
- `--padding-ratio FLOAT`：在精准关键点框基础上按比例扩展正方形裁剪边长，默认 `0.60`；
- `--minimum-side INT`：正方形裁剪边长的最小像素值，默认 `160`。

例如指向另一台机器上的数据集并放宽裁剪边距：

```bash
./scripts/run_python.sh keypoint \
  --dataset-root "/path/to/游泳6拼接1080P-2D关键点标注" \
  --output-dir outputs/keypoint_preview \
  --padding-ratio 0.8 \
  --minimum-side 200
```

## 水下拼接（underwater stitch）

`python/underwater/` 是一个**与六路 pool 流程任务隔离**的新任务，实现「N 块平面水平依次连接」的拼接通路（当前 16 块，早期用 `01d.fbx` 的 2 块验证）。它不复制算法，而是 import 复用 pool 的提取与渲染函数（`python.assets.extract_fbx`、`python.validation.reference_renderer`），产物独立写入 `outputs/underwater/`，不与 pool 交叉。

### 一键实时拼接（macOS + Windows）

给 16 路 `.ts` 片段目录，一条命令跑完「提取网格 → 编译 .swasset → 构建 C++ → 实时渲染」。产物已是最新的步骤会自动跳过，加 `--force` 强制重做。

```bash
# macOS / Linux
./scripts/run_underwater.sh /path/to/swb_20260727-174520_10 --seconds 30 --encode

# Windows
pwsh scripts/run_underwater.ps1 D:\SWIM\swb_20260727-174520_10 -Seconds 30 -Encode
```

两个包装脚本都只是转发到同一份跨平台逻辑 `python/underwater/run.py`，也可以直接调用：

```bash
.venv/bin/python -m python.underwater.run --video-dir DIR --seconds 30 --encode
```

平台差异全部由 `run.py` 处理：macOS 用 Ninja + `metal` 后端 + `build/metal-release/swim_realtime`；Windows 用 Visual Studio 17 2022 (x64) + `d3d11` 后端 + `build/win-d3d11/Release/swim_realtime.exe`（有 CUDA/FFmpeg/GLFW 时可 `--backend cudagl`）。运行时 config 每次按片段目录重新生成到 `inputs/configs/underwater_16_<backend>.conf`，`source.underAi=` 的声明顺序即通道顺序。

常用参数：`--seconds N`、`--encode`、`--no-window`（离屏）、`--fps N`（覆盖渲染帧率）、`--steps asset,run`（只跑部分步骤）、`--config PATH`（用现成 config，不再生成）。

macOS/Metal 实测：16 路 1280×720 MPEG-TS → 6002×722，渲染 30.1fps、解码 4848 帧零 malformed、HEVC 硬件编码 30.1fps、预览零丢帧。相机数量、相机 ID、输出尺寸、解码分辨率全部来自 config 与 `.swasset`，三个后端（Metal / D3D11 / CUDA-GL）共用同一套 `swim_core` 逻辑。

### 分步骤运行

提取 FBX 网格为 JSON。`all.fbx` 含全部 16 块平面，但同时夹带无纹理的支架框、泳道标记条与重复网格；`--planes-only` 只保留「每个纹理一块、位于泳池 Y 带内的全高平面」：

```bash
.venv/bin/python -m python.underwater.extract \
  inputs/underwater/models/all.fbx \
  outputs/underwater/all_mesh.json \
  --tex-dir inputs/underwater/models/all.fbm \
  --planes-only
```

网格按每块世界 X 最小值升序排列（左→右），不依赖 FBX 节点声明顺序。

编译 GPU 运行时资产。`--no-neg-v` 因为水下画面本就正立；`--blend-px` 让烘焙的权重与离线渲染的硬缝一致：

```bash
.venv/bin/python -m python.assets.compile_runtime_asset \
  outputs/underwater/all_mesh.json build/assets/generated/underwater_16.swasset \
  --camera-ids underA16 underA15 underA14 underA13 underA12 underA11 underA10 \
               underA9 underA8 underA7 underA6 underA5 underA4 underA3 underA2 underA1 \
  --ppm 240 --no-neg-v --blend-px 120
```

### 离线渲染（静图 / 视频）

渲染静态拼接图与网格诊断图：

```bash
.venv/bin/python -m python.underwater.render
```

- `--ppm` 默认按世界 X 跨度自适应到约 `--target-width`（默认 640）像素宽，避免对 640×360 源纹理无意义放大；可显式覆盖 `--ppm`。
- `--full-res` 输出高度对齐源图高度、宽度等比缩放；缩放前会**自动砍掉最下方存在黑色（无纹理）像素的整行**（矮平面的透视地面缺口），再等比缩放，避免把黑边拉伸进画面。需要固定裁剪行数时用 `--crop-bottom-px N` 覆盖。
- 默认按正立朝向合成；如需翻转 Y（世界 V）可加 `--neg-v`。
- `--blend-px N` 控制竖直接缝的过渡带宽度，0 为硬切。

**用原图（无网格标注）拼接**：`all.fbm` 里的 `underAi-grid.png` 是标注网格叠加图；每块的「原图像」是各相机的第一帧。`export_real_tex` 复用 `annotation_preview` 的数据集索引，把干净原图按同一 basename 导出到 `outputs/underwater/real_tex_all/`，随后只需把 `--tex-dir` 指过去：

```bash
.venv/bin/python -m python.underwater.export_real_tex
.venv/bin/python -m python.underwater.render \
  --data outputs/underwater/all_mesh.json \
  --tex-dir outputs/underwater/real_tex_all \
  --still outputs/underwater/all_real_stitch_fullres.png --full-res
```

**离线拼接视频（带墙钟时间对齐）**：各路 `.ts` 的第 0 帧不是同一时刻——录制器把关键帧放在 lookback 窗口内的任意位置，GOP 粒度使各路偏差可达数秒。`render_video` 按 manifest 的 `align_start_ms` 与各路 `keyframe_timestamp_ms` 换算每路起始帧，与前端播放器同一套公式：

```bash
.venv/bin/python -m python.underwater.render_video DIR \
  --data outputs/underwater/all_mesh.json \
  --out outputs/underwater/all_sync_stitch.mp4 --blend-px 120
```

manifest 缺失或没有 align 窗口会直接报错退出，不会静默退化；确实需要「各路都从第 0 帧读」时显式加 `--no-align`。文件时长、帧数、大小只作质检，不参与对齐。

## 入水检测机位（water entry）

`python/water_entry/` 是第三类相机的任务：水下 0 号平面正上方的单个 Orbbec 机位（RGB 1280×720 @30fps），用于仰泳蹬壁出发的**空中反弓与入水姿态**识别。它与六路 pool 拼接、16 块水下拼接互不依赖，只共用 `.venv` 与 `outputs/` 约定，产物写入 `outputs/water_entry/`。

数据集默认指向本机路径，可用 `WATER_ENTRY_DATASET_ROOT` 覆盖：

```text
/Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-gz-bad
├── bk_export_manifest.csv          # 115 条片段索引 + 质量标记（先读这个）
├── bk_export_202607/               # <clip>.mp4 与 <clip>_res.json
├── yolo11n-pose-swimup_20250919.pt # 现网特化模型
└── yolo11n-pose-swimup-bk.pt       # 随包微调版
```

### 预测与横向对比

`python.water_entry.predict` 对每条片段只推理「起跳前 5 帧 ~ 入水后 20 帧」的窗口，并把 `swimup`（现网）、`swimup_bk`（微调版）、`coco`（通用 `yolo11n-pose`，按需自动下载）三个模型跑在同一窗口上对比：

```bash
./scripts/run_python.sh we-predict                       # 全部 115 条 × 3 模型
./scripts/run_python.sh we-predict --limit 5 --models swimup_bk
./scripts/run_python.sh we-predict --clips 20260725-160224 --conf 0.05
```

产物：

- `outputs/water_entry/predict/<model>/metrics.csv`：逐片段指标；
- `outputs/water_entry/predict/<model>/per_frame/<clip>.json`：逐帧框、17 点关键点与置信度。

四个关键指标：`det_rate`（窗口检出率）、`flight_rate`（起跳→入水的空中段检出率）、`entry_rate`（入水帧 ±3 帧检出率）、`pred_entry_frame`（按「肩中点与胯中点上下关系翻转」判据估计的入水帧）及其与基准的 `entry_delta`。

**基准入水帧取 `res.json` 的 `metadata.backstroke.entry_frame`，不是 `manifest.csv` 的 `water_frame`。** 在 `backstroke_applied=False` 的 47 条片段上，`water_frame` 比真入水早 3~36 帧（中位 28 帧）——它来自 water_line 掩膜扫描，运动员还在扶壁蜷缩时就会命中。人工抽帧核对过 `20260717-101123`（manifest 88 / backstroke 119），f88 时人仍在池壁，真入水在 f119 附近。两个口径都写进 metrics.csv 供对照，窗口取两者并集。

### 复核页

`python.water_entry.review` 不重新推理，只读 per_frame JSON，把入水帧 ±3 帧渲染成叠加骨架的裁剪图，一行一帧、行内按模型横排：

```bash
./scripts/run_python.sh we-review --clips 20260725-160224 20260717-101123 --limit 0
```

打开 `outputs/water_entry/review/index.html`。同一行的各模型共用一个裁剪中心（各模型检出框中心的均值），因此「A 检出、B 缺检」两格看的是同一块画面；红框表示该模型此帧缺检，橙点为肩中点、粉点为胯中点，`sho-hip` 由负转正即入水判据触发点。

### 本次实测结果（115 条全量，93 条 note 为空的干净片段）

| 模型 | flight_rate | entry_rate | flight<0.9 的片段 | 入水帧 \|Δ\|≤2 |
| --- | ---: | ---: | ---: | ---: |
| `swimup`（现网） | 0.944 | 0.959 | 8 | 71/93 |
| `swimup_bk`（微调版） | **1.000** | **0.998** | **0** | 70/93 |
| `coco`（通用） | 0.739 | 0.482 | 60 | 17/93 |

微调版在空中段与入水段都是满检出，交付说明里「现网模型飞行段完全失明」在全量上表现为局部失明：`swimup` 有 2 条整窗零检出（`20260713-173110`、`20260727-101601`）、4 条 `flight_rate<0.5`，其余片段仍能跟住。通用 COCO 模型的表现与说明相反——它在**入水段**最差（`entry_rate` 仅 0.48，60 条片段空中段检出不足 0.9），说明拿它做自动预标注时，触水前后那几帧仍需人工补。

入水帧判据在三个模型上都不是即插即用：微调版逐帧检出满分，但 `|Δ|≤2` 只有 70/93，中位偏差 0 帧、尾部偏差可达十几帧。判据本身在人工标定的两条片段上是准的，偏差来自选人——前排泳道游进的人与岸上教练会抢走轨迹。当前选人只用「起跳后沿游进方向净位移最大 + 轨迹最长」，还没接入 ROI 泳道约束（`res.json` 的 `metadata.roi` 有 trigger/assist/ignore 分区可用）。

MPS 后端偶发把整窗推理返回全零检测（实测复现 1 次，重跑即恢复）。`predict.py` 因此分批推理，且在窗口全空时自动用 CPU 复算一遍再定论，`metrics.csv` 的 `fallback` 列记录是否触发过复算——GPU 抖动不应被记成模型失明。

### 挑选增量标注数据

`python.water_entry.select_frames` 只比较 `swimup` 与 `swimup_bk`（待标注数据是给这两个模型做增量训练的，通用 COCO 的失效模式与我们的训练集无关），逐帧命中七类「模型做得差」的信号：

| 信号 | 含义 | 基础分 |
| --- | --- | ---: |
| `both_blind` | 两个模型都 0 检出 | 100 |
| `both_reject` | 有检出但选人都没接上 | 70 |
| `one_miss` | 只有一个模型检出 | 60 |
| `diff_person` | 两框 IoU 低于阈值，指向不同的人 | 55 |
| `sign_flip` | 两模型对 sho-hip 符号判断相反 | 50 |
| `kp_disagree` | 同一人但关键点平均分歧超阈值 | 30 |
| `torso_broken` | 有框但躯干四点不全 | 25 |

`score` 取命中信号的最大基础分（而非求和，避免一堆弱信号压过一个强信号），叠加其余信号一成加成，再乘阶段权重：入水±3帧 1.6、飞行段 1.25、入水后 1.0、起跳前 0.5。

```bash
./scripts/run_python.sh we-select                       # 默认口径，写候选 CSV
./scripts/run_python.sh we-select --kp-mean-norm 0.10   # 收紧，只要最显著的分歧
./scripts/run_python.sh we-annotate --limit 100          # 前 100 帧质检页
```

产物 `outputs/water_entry/annotate_candidates.csv`（按分数降序），以及质检页 `outputs/water_entry/annotate_preview/index.html`——每候选帧一行，左中两格是两个模型的骨架叠加，右格是标注员实际要看的无叠加原图。

筛选口径的默认值集中在 `select_frames.py` 的 `DEFAULT_*` 常量里，`scripts/run_water_entry.sh` 启动时读取它们而不是复制一份，所以「走流程脚本」与「直接调 `we-select`」永远给出同一批候选帧。三个值都是抽帧核对后定的，不是拍脑袋：

- **`--max-offset 6`**：入水 6 帧之后运动员已没入水面，两模型开始各自锁住不同的水花伪影。实测两框 IoU<0.3 的帧在 offset +6~+12 占 16.6%、+13 之后占 75.3%，而入水帧前后 ±5 帧一个都没有——那种分歧不是姿态质量问题，人工也标不出关键点。
- **`--min-gap 1`（不去重）**：相邻帧画面相似但姿态在变，训练时这种差异有价值。去重只为人工翻页方便，需要时用 `--min-gap 3` 压到约三分之一。
- **默认排除 `entry_source != "backstroke"` 的 4 条片段**（`20260707-105111`、`20260713-173110`、`20260721-162634`、`20260727-101601`）。它们的基准入水帧退化成 manifest 的 `water_frame`，抽帧确认过偏早若干帧（`20260707-105111` 标 f93，实际 f98 之后才入水），偏移量与阶段权重都不可信。其中 `20260713-173110` 与 `20260727-101601` 窗口内根本没有出发动作——`swimup_bk` 选中的是岸上走动的人，`swimup` 什么都没选。需要纳入时加 `--allow-unverified-entry`。

排除 17 条 `suspected_false_positive` 与上述 4 条后，94 条片段共 2613 个窗口内帧，当前默认口径选出 **1163 帧，覆盖全部 94 条片段**（每片段 min 3 / 中位 10 / p90 20 / max 36），阶段分布 entry 479 / flight 314 / post 249 / pre 121。信号分布：`kp_disagree` 1042、`sign_flip` 107、`one_miss` 90、`diff_person` 4、`both_blind` 3、`torso_broken` 3。

信号在时间上的分工很清楚（`frame - entry_frame` 中位值）：`sign_flip` +2、`kp_disagree` +6，两者集中在入水帧附近，是最有价值的；`both_blind` +17、`diff_person` +12、`torso_broken` +12 则几乎全在入水后，被 `--max-offset 6` 截掉后只剩零星几帧。`torso_broken` 只在 `swimup_bk` 侧触发（`swimup` 的选人硬要求躯干四点，所以它永远是 4/4）——它更像 bk 模型的水下伪影，不是独立的质量信号。

**82% 的候选来自 `kp_disagree` 这一个信号**，意味着绝大多数帧两模型的框和选人都是对的，差的只是关节点精度。派给标注员时应强调「在预标注基础上精修关键点」，而不是重画框。

`--kp-mean-norm` 是控制产出量的主要旋钮（其余六类信号是离散判定、无阈值）。分歧值本身的中位数是 0.0497，所以阈值压到 0.05 以下等于「一半的帧都算难例」，不再是筛选：

| `--kp-mean-norm` | 候选帧 | 占窗口内 2613 帧 |
| ---: | ---: | ---: |
| 0.10 | 323 | 12.4% |
| 0.06 | 936 | 35.8% |
| **0.055（当前默认）** | **1163** | **44.5%** |
| 0.05 | 1486 | 56.9% |

当前 1163 帧覆盖全部 94 条片段，每片段中位 10 帧、p90 20 帧。抽帧核对过新纳入的两个分歧带：`0.06~0.08`（439 帧）质量良好，多为一侧模型把肢体关键点画成麻花；`0.05~0.06`（550 帧）开始出现两骨架肉眼近乎重合、仅框大小不同的帧，边际价值较低——这也是没有取到 0.05 的原因。

### 交付包与全流程脚本

`python.water_entry.export_package` 把候选帧导出成可直接交付标注的数据包：**无叠加原始帧** + `manifest.csv` + COCO keypoints 格式的模型预标注 + 交付说明。质检页那套骨架叠加只用于我们自己判断该不该标，真送标注时叠加线条会干扰标注员。预标注优先取 `swimup_bk`、缺检时退回 `swimup`；置信度低于 `KP_CONF` 的点写成 COCO 的 `v=0`，标注工具会显示为「待补」而不是一个错误的既有点。

```bash
./scripts/run_water_entry.sh                  # 全流程：预测 -> 选帧 -> 质检页 -> 交付包
./scripts/run_water_entry.sh --skip-predict   # 复用已有预测，只重跑后续
./scripts/run_water_entry.sh --kp 0.10        # 收紧阈值，选出更少
```

**新增片段后重跑这一个脚本即可全量刷新**：`manifest.csv` 是唯一的片段清单来源，`predict` 会把新片段一并纳入，后续每步都从 `predict` 的产物重算，流程内没有增量状态，不存在只更新一半的可能。

本次产出 `outputs/water_entry/annotate_package.zip`：1163 张图、94 条片段、286 MB，1160 帧带 `swimup_bk` 预标注（每框可用关键点中位 15 个），3 帧两模型都没检出、需从零标注。

## 已知限制

- 视频渲染会读取各路源 FPS，以最低源 FPS 作为输出帧率，并对较高帧率输入按最近目标帧位置抽帧。这只对齐帧率，不会同步各路视频的采集起始时间。
- 渲染器不会读取外部数据集中的 sync map，也不会补偿相机时钟偏移；需要时间同步时，应在渲染前准备好已经对齐的六路输入。
- 六路输入数量必须与六块网格一致，且必须保持 `cam3 cam2 cam1 cam4 cam5 cam6` 的固定位置顺序。
- H.264 的 `yuv420p` 要求偶数宽高，视频编码阶段可能在静态画布右侧或底部补一个像素。
- 默认外部数据集路径是本机绝对路径。换机器或移动数据集后，必须设置 `SWIMMING_DATASET_DIR`（入水检测机位另用 `WATER_ENTRY_DATASET_ROOT`）。
- 入水检测机位的选人只用位移与轨迹长度，未接入 `res.json` 的 ROI 泳道约束。实测选人错误只有 2 例（`20260713-173110`、`20260727-101601`，`swimup_bk` 选中岸上走动的人），且两条片段窗口内本就没有出发动作、已被 `select_frames` 默认排除；但两模型对同一人给出差异极大的框在入水 +6 帧之后很常见，那属于水下伪影而非选人缺陷。
- `link_tracks` 的匹配半径固定为画宽的 15%，刻意不随断裂帧数放大。放大版实测让 `swimup` 的 12 条片段空中段检出下降、10 条归零（轨迹跨缺口接到画面里的静止目标）。改这个参数前请先用 `predict` 全量复跑对比 `flight_rate`。
- `select_frames` 的信号是模型间分歧，只是错误的**代理**而非错误本身：两模型一致犯错的帧不会被选出。
- `suspected_false_positive` 这个标记不能用来解释坏结果：15/17 条的选人几何完全正常（位移 +322~+470 px），说明那些片段里确实有人跳水，上游为何判为误触发在选人层面看不出来。
- `.venv/` 只保证当前 macOS / Python 3.10 组合；其他平台需要准备兼容的 Python、FBX SDK、NumPy、OpenCV 和 FFmpeg 环境。
