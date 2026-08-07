# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

把泳池 / 泳道的 FBX 平面网格与 UV 编译成 GPU 资产，再把多路相机实时拼成一张全景图；附带三类标注与评测工具。C++20 实时核心（`cpp/core/`）+ Python 离线资产链，三个原生后端（Metal / D3D11 / CUDA-GL）共用同一份 `swim_core` 逻辑，通过 `cpp/core/include/swim/core/backend.hpp` 的 `IBackend`/`ISource`/`IRenderer` 契约接入，由 `cpp/app/main.cpp` 显式注册。刻意不做静态初始化自注册；后端专有类型不允许泄漏进 `cpp/core/`。

本仓库存在一份非常详尽的仓库指南 `AGENTS.md`，权威口径以它和 README 为准。以下是最关键、最容易被新会话踩到的部分。

## 四条互不交叉的链路（一个脚本一条）

| 链路 | 入口 | 代码 |
| --- | --- | --- |
| 相机拼接（pool / underwater / overhead 三条相机线） | `scripts/run_stitch.{sh,ps1}` | `python/stitch/` + `cpp/` |
| 入水检测机位 | `scripts/run_water_entry.sh` | `python/water_entry/` |
| 数据集标注 | `scripts/run_label.sh` | `python/labeling/`、`python/keypoints/` |
| 性能取证 | `scripts/run_bench.sh` | `python/benchmarks/` |

- 改哪条链路只看哪条；共用 `python/common/`（路径 / 图像 IO / CSV / HTML）与 `.venv`。仅一处跨链路依赖：`python/stitch/export_ref_tex.py` import `python.labeling.snapshots`。
- 三个 `.sh` 入口无参数或 `--help` 打印自身用法；`run_win.bat` / `install.bat` 是双击入口，无参数即执行。新增脚本延续「一条链路一个入口」口径。
- 另有本次加入的网格检查入口 `scripts/run_fbx_overlay.sh`（water-entry FBX 网格叠加，见下）。

## 相机拼接关键口径

一套代码服务三条相机线，差异全部是 `python/stitch/profiles.py` 里的数据；**不要在 `python/stitch/` 其他模块按线路名分支**。加第四条线应只加一条 profile 记录。

- `Profile` 是 frozen dataclass：`fbx`、`tex_dir`、`camera_ids`、`ppm`、`source_size`、`blend_px`、`clip_uv`、`neg_v`、`order`、`planes_only` 等。`camera_ids` 按顺序配 mesh，**相机身份是位置对应的**，不解析贴图文件名——改 FBX 平面相对位置或 id 顺序会静默错配。
- pool 必须保持 FBX 声明序（两排网格，按 X 排会交错）；另两条线单排，按世界 X 升序。
- `select_planes` 的 world-Y band 只为 underwater `all.fbx` 写的硬编码，不是通用选择器；overhead 的 `002.fbx` 不能过滤。
- `neg_v`：pool 的 bake 存 Y 向下需翻转；plane 线直立不翻转。配错表现为静默镜像。
- 步骤：`extract`（FBX→`outputs/<line>/mesh.json`）、`tex`、`still`（静图+网格诊断+融合热图）、`video`、`asset`（→`build/assets/generated/<line>.swasset`）、`build`、`live`。`extract`/`asset`/`build` 按 mtime/口令跳过，`still`/`video` 每次都渲。
- 运行时 config 每次按片段目录重新生成到 `inputs/configs/<line>_<backend>.conf`，声明顺序即通道顺序，不手工维护。
- `.swasset` v1 格式真值：Python 在 `python/stitch/asset_format.py`，C++ 在 `cpp/core/src/asset.cpp` 与 `cpp/core/include/swim/core/asset_format.hpp`。改动几何/资产编译的验收是**逐字节**一致（比 sha256）。

## FBX SDK 与 Python 版本约束

- **Python 3.10 是硬要求**：Autodesk 只为 cp310 发布 FBX SDK 轮子，`python/fbx_tools/scene.py` 是模块级 `import fbx`。
- `fbx_tools/` 是**唯一**允许 `import fbx` 的包（`tests/python/test_layout.py` 断言）。只读已提取 mesh JSON 的代码必须在没有 `fbx` 的机器上也能跑。
- FBX 读取关键函数（`python/fbx_tools/scene.py`）：`read_scene`（返回 manager/scene/nodes，**调用方负责 `manager.Destroy()`**）、`node_matrix`（含 geometric transform）、`material_index`（多材质按 polygon 取实际材质）、`diffuse_texture`、`uv_element`/`uv_at`（处理 mapping/reference mode）、`extract_mesh`（输出 2-D 三角形 + UV，fan 三角化）。
- 新 Python 包必须登记进 `tests/python/test_layout.py` 的 `PACKAGES`，且 `__init__.py` 有 docstring；除 `common/paths.py` 外任何模块不得自己算仓库根。

## 常用命令

```bash
# Python 测试（仓库没有 pytest，用 unittest）
.venv/bin/python -m unittest discover -s tests/python -t .
# 单测文件
.venv/bin/python -m unittest tests.python.test_stitch
# 单测类/方法
.venv/bin/python -m unittest tests.python.test_stitch.CanvasTest
# 真实 FBX 集成测试在缺 SDK 或缺本地模型时自动跳过

# C++（macOS，Metal；Windows 见 README/AGENTS）
cmake --build build/metal-release --target swim_core_tests && ./build/metal-release/swim_core_tests
cd build/metal-release && ctest

# 拼接线
./scripts/run_stitch.sh pool extract,still
./scripts/run_stitch.sh underwater extract,asset,build,live --video-dir /path/to/dir --seconds 12
./scripts/run_stitch.sh overhead tex,still --real --video-dir /path/to/dir

# 入水检测全流程
./scripts/run_water_entry.sh
./scripts/run_water_entry.sh --skip-predict   # 复用已有预测

# water-entry FBX 网格叠加（一键三张）
./scripts/run_fbx_overlay.sh [输出目录]

# 快照整理/合成/标注/拼接统一入口（水下16 + overhead + femto/gemini）
./scripts/run_frames.sh organize                      # 整理所有相机；水下额外差分筛选
./scripts/run_frames.sh auto_merge --camera underA1   # 自动合成（中值+差分），--camera 必填
./scripts/run_frames.sh merge                         # 手动合成（mask 前景+中值背景）
./scripts/run_frames.sh grid                          # 水下 4×4 拼接（每帧标帧ID+米数）
./scripts/run_frames.sh label                         # 打开浏览器 mask 标注器
```

环境：`.venv`（Python 3.10）+ `requirements-win.txt`（numpy / opencv-python-headless）+ Autodesk FBX SDK + `requirements-pose.txt`（torch/ultralytics，约 2.5GB，**仅**入水检测链路需要，由 `install.bat pose` 安装）。macOS 手工准备检查：`.venv/bin/python -c "import fbx, numpy, cv2, PIL"`。

## 入水检测链路（water_entry）

单机位 YOLO-pose 难例筛选：`predict.py` → `select_frames.py` → `annotate_preview.py` → `export_package.py`（`scripts/run_water_entry.sh` 顺序调用；`review.py` 是基于已有 per-frame JSON 的独立复核入口）。manifest.csv 是片段唯一来源，后续步骤全量重算、无增量状态。

- 数据集根默认 `/Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-up/swimming-gz-bad`，用 `WATER_ENTRY_DATASET_ROOT` 覆盖；产物默认 `outputs/water_entry/`，用 `WATER_ENTRY_OUTPUT_ROOT` 覆盖。
- 判据集中在 `common.py`：入水帧取 `backstroke.entry_frame`（`res.json`）优先，`manifest.water_frame` 只作对照——`water_frame` 在 `backstroke_applied=False` 时不可信。
- 筛选阈值唯一来源是 `select_frames.py` 的 `DEFAULT_*`，`run_water_entry.sh` 运行时读取而非复制。

## water-entry FBX 网格叠加（fbx_overlay）

新增链路（本分支）：`scripts/run_fbx_overlay.sh` → `python -m python.fbx_overlay`（`python/fbx_overlay/`）。把 FBX 的归一化 UV 画到底图上检查网格与图像对齐。

- 固定输入：底图 `inputs/water_entry/background.jpg`；`006.fbx`→`Plane004`（垂直水面，140 tris）、`005.fbx`→`Plane005`（水面，60 tris）。
- 默认 UV V 原点 `bottom`（FBX 惯例，像素 `y=(1-v)*(height-1)`）；模型按图像坐标制作时用 `--uv-v-origin top`。坐标不钳制，越界交给 OpenCV 裁剪。
- 节点严格按名匹配：缺失/重名时报错并列出实际节点，禁止退化为取第一个 mesh。
- 渲染纯函数在 `render.py`（不 import fbx，可无 SDK 测试）；CLI 在 `__main__.py` 负责场景生命周期与节点筛选。默认底图/模型已写入 CLI，无需重复传参。
- 本机 FBX SDK 读取 005/006 会生成 `.mayaSwatches/*.swatch` 缓存文件，属本地产物，不要提交。

## 快照整理/合成/标注/拼接（frames）

`scripts/run_frames.sh` → `python -m python.labeling.frames`（`python/labeling/frames.py`），五条子命令共用同一入口：

- `organize`：所有相机按时间序整理成帧文件夹（`f<NN>_<snapshot>__<orig>.jpg`，字节级拷贝）；水下 16 相机额外做中值背景差分筛选。
- `auto_merge --camera X`：自动合成（中值背景 + 差分前景叠加，流式分带内存封顶），相机必填。
- `merge`：手动合成，读 `mask_label_project.json`，mask 覆盖处取原帧、其余取中值背景，处理工程里所有相机。
- `grid`：仅水下，16 相机 mask 合成图 4×4 cat 拼接（纯可视化），每格标相机 ID + 泳道米数，工程给定时再标每帧 mask 的帧 ID + 米数。
- `label`：起浏览器 mask 标注器（选目录即通用：overhead/underwater/femto/gemini）。

- 数据根 `<数据集根>/<date>/snapshots/`，产物统一 `<数据集根>/<date>/object-frames/`（与 20260708 的 object-frames 平级）：每相机一个目录、`detections.csv`（全帧差分统计）、`curated.csv`（is_object=1 的值得标注帧）。
- 差分口径对齐旧 detections.csv：`score_frac_gt40` 是该帧与相机逐像素中值背景的 RGB 欧氏距离 > 40 的像素占比；`cam_median` 是全时段 score 中位数；`threshold = cam_median × 1.28`（对齐旧数据 ~34% 精选率）；`is_object = score > threshold`。
- `snapshots.py` 的 `frames_for_camera(camera, date=None)` 支持日期层：`date` 缺省走旧布局 `<root>/snapshots`，传入则走 `<root>/<date>/snapshots`；frames 的 `frames_for_camera` 是本模块口径（可 patch 测试）。
- 中值/差分按水平条带算，峰值内存 ≈ 帧数 × 带高 × 宽 × 3，`--band-rows` 调小可压内存。

## 编码风格与提交

- Python：4 空格、小写下划线，模块入口一律 `python -m python.<pkg>[.<mod>]`。C++20：`swim::core` / `swim::d3d11` / `swim::cudagl`，成员变量带尾下划线。
- 源码 UTF-8；注释解释**为什么**，尤其是「看起来可以简化但实际不能」的地方。
- `scripts/**/*.bat` 必须 UTF-8 无 BOM + CRLF，第三行 `goto :run`，中文只放在被 goto 跳过的说明区，执行区注释一律 ASCII；改动后运行 `scripts\checks\check_bat_format.ps1`。
- 提交信息 `type(scope): 简短祈使句`，与现有历史一致。
- 不提交：`build/`、`outputs/`、`third_party/`、`*.pt`、大视频、`.swasset`、`.venv`、`inputs/{underwater,overhead}/models/`。`outputs/pool/mesh.json` 被 gitignore 却是 CMake 硬依赖（缺了任何 target 都编不过），用 `./scripts/run_stitch.sh pool extract` 生成。
