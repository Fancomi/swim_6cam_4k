# Swim FBX Demo

## 项目简介

本项目把泳池 FBX 中的平面网格与 UV 映射提取为 JSON，再使用六路相机的合成纹理或视频帧生成俯视泳池拼接图像和 H.264 视频。FBX SDK 只参与 UV 烘焙和网格提取；常规渲染由 NumPy、OpenCV 和 FFmpeg 完成。

本文所有命令都假定当前目录是项目根目录 `swim_fbx_demo/`。

## 实时 Metal 路径

仓库同时包含独立的 macOS 实时实现：六路 H.264 由 VideoToolbox 解码为 GPU 可见表面，Metal 以固定六网格合成 `5002x2102` 输出，并可分流到 preview 与硬件 HEVC。该运行路径不依赖 OpenCV 或 FFmpeg，不把解码像素读回 CPU；各路输入采用容量有界的 latest-frame 交换，接收端只消费当下最新完整帧。

代码按语言隔离：实时核心位于 `cpp/core/`，Apple 原生后端位于 `cpp/backends/metal/`，离线资产与验证工具位于 `python/`，运行入口位于 `scripts/`。默认现场数据集仍是：

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

`demo` 默认 `--preview-visible=true`，会创建 AppKit/CAMetalLayer 窗口显示实时拼接画面，并把硬件 HEVC 写到 `outputs/videos/pool_metal.h265`，指标写到 `benchmarks/manual.jsonl`。`--no-window` 不是跳过 preview：它仍创建私有 Metal render target，对每个被接受的最新输出执行真实 shader copy/render command，并等待 GPU completion。benchmarks/soak 默认无窗口；需要窗口时加 `--visible`。

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

结果位于 `benchmarks/runs/<run_id>/`，成功后 `benchmarks/latest` 指向该目录：

- `cells/*.jsonl`：带唯一 `(stage, stream_count, pacing)` 身份的原始 cell；
- `results.jsonl`：48 个 cell 全部通过后才生成的合并记录；
- `summary.csv`、`summary.md`：最终吞吐、区间 p50/p95、瓶颈排名及 preview/encode 增量成本；
- `manifest.json`：run/build/hash 身份与 `publishable` 标志；不足 15 秒始终为 `false`。

可单独复验已生成的矩阵：

```bash
.venv/bin/python -m python.validation.summarize_benchmarks \
  benchmarks/latest/results.jsonl
```

默认十分钟的六路 paced full soak：

```bash
./scripts/run_metal.sh soak
```

soak 按每条 interval 的真实 `elapsed_s` 累加时间轴，报告 RSS 与 Metal allocation 的每分钟线性斜率，并拒绝 host copy、容量 high-water 越界、编码 callback/drain 错误，以及 warm-up 后连续五个区间低于 29 FPS。默认 RSS 增长上限为 64 MiB/min，Metal allocation 上限为 32 MiB/min；可用 `--max-rss-slope` 和 `--max-gpu-slope` 显式覆盖。

## 处理流程

1. `./scripts/run_python.sh bake ...` 可选地把中线 UV 延伸写入一个新的 FBX，原始模型不需要被覆盖。
2. `./scripts/run_python.sh extract` 读取 `inputs/models/pool.fbx`，提取三角形、二维位置和 UV，默认写入 `outputs/data/pool_mesh.json`。
3. `./scripts/run_python.sh still` / `4k` 使用网格 JSON 与纹理或六路视频生成静态拼接图或 H.264 视频。
4. `./scripts/run_python.sh asset` 把网格 JSON 编译为 Metal 运行时 `.swasset`。
5. `./scripts/run_python.sh keypoint` 生成 2D 关键点裁剪复核页。

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
│   └── tests/
│       ├── __init__.py
│       ├── test_keypoint_preview.py
│       └── test_layout.py
├── scripts/
│   ├── run_metal.sh              # demo / benchmarks / soak
│   └── run_python.sh             # still / 4k / keypoint / extract / bake / asset
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

项目现有 `.venv/` 是面向 macOS 和 Python 3.10 的环境，不应直接复制到其他操作系统或其他 Python 版本使用。可先检查当前环境：

```bash
.venv/bin/python --version
.venv/bin/python -c "import fbx, numpy, cv2; print('Python dependencies OK')"
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
  inputs/models/pool.fbx \
  outputs/data/pool_mesh.json \
  --tex-dir inputs/textures
```

如需把中线 UV 延伸先烘焙到一个新模型，再提取该模型，可运行：

```bash
.venv/bin/python -m python.assets.bake_uv \
  inputs/models/pool.fbx \
  outputs/data/pool_uv_baked.fbx \
  --ext-px 5 \
  --tex-dir inputs/textures

.venv/bin/python -m python.assets.extract_fbx \
  outputs/data/pool_uv_baked.fbx \
  outputs/data/pool_mesh.json \
  --tex-dir inputs/textures
```

第一条命令保留 `inputs/models/pool.fbx`，并在已有的 `outputs/data/` 目录创建新 FBX；第二条命令更新渲染器默认使用的网格 JSON。

## 生成静态图和网格预览

生成完整分辨率的静态拼接图和网格叠加图：

```bash
.venv/bin/python -m python.validation.reference_renderer \
  --data outputs/data/pool_mesh.json \
  --tex-dir inputs/textures \
  --still outputs/images/pool.png \
  --grid-still outputs/images/pool_grid.png
```

降低每米像素数可更快生成网格预览：

```bash
.venv/bin/python -m python.validation.reference_renderer \
  --data outputs/data/pool_mesh.json \
  --tex-dir inputs/textures \
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
| 1 | `01` | `inputs/textures/camera_3_composite.png` | `20260629_172532_cam3.mp4` |
| 2 | `02` | `inputs/textures/camera_2_composite.png` | `20260629_172532_cam2.mp4` |
| 3 | `03` | `inputs/textures/camera_1_composite.png` | `20260629_172532_cam1.mp4` |
| 4 | `u` | `inputs/textures/camera_4_composite.png` | `20260629_172532_cam4.mp4` |
| 5 | `Plane004` | `inputs/textures/camera_5_composite.png` | `20260629_172532_cam5.mp4` |
| 6 | `Plane007` | `inputs/textures/camera_6_composite.png` | `20260629_172532_cam6.mp4` |

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

## 已知限制

- 视频渲染会读取各路源 FPS，以最低源 FPS 作为输出帧率，并对较高帧率输入按最近目标帧位置抽帧。这只对齐帧率，不会同步各路视频的采集起始时间。
- 渲染器不会读取外部数据集中的 sync map，也不会补偿相机时钟偏移；需要时间同步时，应在渲染前准备好已经对齐的六路输入。
- 六路输入数量必须与六块网格一致，且必须保持 `cam3 cam2 cam1 cam4 cam5 cam6` 的固定位置顺序。
- H.264 的 `yuv420p` 要求偶数宽高，视频编码阶段可能在静态画布右侧或底部补一个像素。
- 默认外部数据集路径是本机绝对路径。换机器或移动数据集后，必须设置 `SWIMMING_DATASET_DIR`。
- `.venv/` 只保证当前 macOS / Python 3.10 组合；其他平台需要准备兼容的 Python、FBX SDK、NumPy、OpenCV 和 FFmpeg 环境。
