# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概览

把泳池 / 泳道的 FBX 平面网格与 UV 编译成 GPU 资产，再把多路相机实时拼成一张全景图；附带三类标注与评测工具。C++20 实时核心（`cpp/core/`）+ Python 离线资产链，三个原生后端（Metal / D3D11 / CUDA-GL）共用同一份 `swim_core` 逻辑，通过 `cpp/core/include/swim/core/backend.hpp` 的 `IBackend`/`ISource`/`IRenderer` 契约接入，由 `cpp/app/main.cpp` 显式注册。刻意不做静态初始化自注册；后端专有类型不允许泄漏进 `cpp/core/`。

本仓库存在一份非常详尽的仓库指南 `AGENTS.md`，权威口径以它和 README 为准。以下是最关键、最容易被新会话踩到的部分。

## 标定数据不在 git 里

`inputs/` 的 225MB 标定数据（两代 FBX + 实拍贴图）**已于 2026-08-17 从全部历史抹除**，`.git` 从 60MB 降到 1.8MB。**不入库，也不上 LFS**——GitHub 免费额度是 1GiB 存储 + 1GiB/月流量，一次完整 clone 就吃掉 225MB 流量，而这批数据按 FBX 版本迭代（underwater2 已四版），每版都存就是每版一份 61MB 副本。数据走带外搬运，仓库只留代码与验收依据。

- 唯一入库的 inputs 是 `inputs/configs/` 的手写参考 config（`macos_*` / `windows_*`）；`.gitignore` 是 `inputs/*` 全忽略再放行它们，**不要再往 inputs 里加 `!` 例外**。
- 两代差异、目录结构、按链路最少搬多少：`docs/DATA.md`。搬完用 `./scripts/check_inputs.sh [v1|v2]` 验收，它分辨 MISSING / TRUNCATED / **CONTENT**（同大小不同内容——贴图版本错了，照样能跑但缝会错位，最危险的一种）。
- 验收清单 `docs/data-manifest.tsv` 的**路径由 profiles 导出、不手写**（`python/dataset/manifest.py`）；加一条线后 `./scripts/check_inputs.sh --write` 重新生成，校验脚本不用改。
- **只想编 C++ 也得先有 pool 的一代数据**：`outputs/pool/mesh.json` 是 CMake 硬依赖，而它由 `run_stitch.sh pool extract` 从 `pool.fbx` 生成。

## 四条互不交叉的链路（一个脚本一条）

| 链路 | 入口 | 代码 |
| --- | --- | --- |
| 相机拼接（pool / pool2 / underwater / underwater2 / overhead / overhead2 六条相机线） | `scripts/run_stitch.{sh,ps1}`、`scripts\run_win.bat` | `python/stitch/` + `cpp/` |
| 入水检测机位 | `scripts/run_water_entry.sh` | `python/water_entry/` |
| 数据集标注 | `scripts/run_label.sh` | `python/labeling/`、`python/keypoints/` |
| 性能取证 | `scripts/run_bench.sh` | `python/benchmarks/` |

- 改哪条链路只看哪条；共用 `python/common/`（路径 / 图像 IO / CSV / HTML）与 `.venv`。**跨链路依赖只有两处**，都登记在 `tests/python/test_layout.py` 的 `CROSS_CHAIN_IMPORTS` 里（新增一条得先改那个断言）：`python/stitch/export_ref_tex.py` import `python.labeling.snapshots`（水下线没有按次片段可采首帧）；`python/stitch/extract.py` import `python.fbx_overlay.meters`（俯视线的网格就是标定物，米数规则不复制第二份）。
- 三个 `.sh` 入口无参数或 `--help` 打印自身用法；`run_win.bat` / `install.bat` 是双击入口，无参数即执行。新增脚本延续「一条链路一个入口」口径。
- 另有两个辅助入口：`scripts/run_fbx_overlay.sh`（入水机位网格叠加，见下）与 `scripts/check_inputs.sh`（标定数据验收，`python/dataset/`）。

## 相机拼接关键口径

一套代码服务六条相机线，差异全部是 `python/stitch/profiles.py` 里的数据；**不要在 `python/stitch/` 其他模块按线路名分支**。加一条线应只加一条 profile 记录。三个物理机位、六条线：同一批相机换一份重建的 FBX 就是一条新线（`pool`/`pool2`、`underwater`/`underwater2`、`overhead`/`overhead2`），不是一个新步骤。

- `Profile` 是 frozen dataclass：`fbx`、`tex_dir`、`camera_ids`、`ppm`、`source_size`、`blend_px`、`clip_uv`、`neg_v`、`neg_u`、`order`、`planes_only`、`ref_tex`、`label_line` 等。`camera_ids` 按顺序配 mesh，**默认相机身份是位置对应的**，不解析贴图文件名——改 FBX 平面相对位置或 id 顺序会静默错配。
- **新版 FBX 接入顺序**（pool2 / underwater2 都是这么定的，别跳步）：① `extract` 看节点数、贴图名、常量轴；② **相机身份用贴图像素相关认**（贴图 vs 片段首帧灰度归一化相关），`.fbm` 文件名可能是从别处复用的，按世界位置猜会被朝向差异整体带偏、症状伪装成「UV 标歪了」；③ 把每台相机中心归一化成画布比例与旧线比对，定 `neg_u`/`neg_v`；④ 加一条 profile 记录。
- pool 必须保持 FBX 声明序（两排网格，按 X 排会交错）；平面线单排，按世界 X 升序。
- `select_planes` 的 world-Y band `(-11.6, -8.0)` 只为 underwater `all.fbx` 写的硬编码，不是通用选择器。overhead 的 `002.fbx`（Y `[20.47, 23.47]`）、overhead2 的 `25 水面.fbx`（Y `[31.94, 34.94]`）和 underwater2 的 `8.15.fbx`（Y `[-10.09, -7.34]`）都**不能**过滤，开了会把平面全丢掉、只报「no pool plane found」。
- **只挪顶点的 FBX 改版不是新线**：换 `fbx`/`tex_dir` 两个字段即可（underwater2 已从 `ALL OK.fbx` → `ALL OK- 8.14.fbx` → `ALL OK- 8.14-02.fbx` → `8.15.fbx`：整体去掉最下 0.25m，A16/A9 各短 0.5m；8.14-02 又把 A1 从网格里去掉——15 个节点 02..16，`camera_ids` 少一台，贴图从 mask 合成图换成裸背景 `underA*_background.png`，所以这一版 `camera_ids` 也变了；8.15 一个顶点没挪，只改 A10 一块：UV 下移约 5px/3.8cm 重新对准，贴图换成 `10.png`——那是样本 `swb_20260813-170549_24` 的 A10 片段首帧，不是干净背景，所以 `.fbm` 出的 `still` 里只有 A10 带泳者水花）。必须 `extract --force`（mtime 跳过看不出换了文件），并复核画布尺寸与 `blend_px` 是否还配得上新的重叠宽度。
- **只换了贴图/UV 的改版怎么验**：让两版网格渲染**同一批**外部贴图（`still --tex-dir <数据集> --tex-pattern '{camera}_background.png' --still-suffix _vNNN`），把贴图这个变量固定住，剩下的差异只能来自几何/UV。逐块量最佳位移：没动的块 corr 1.0000/dy 0，动过的块会明确报出 dy。
- `neg_v`：pool 的 bake 存 Y 向下需翻转；plane 线直立不翻转。`neg_u` 翻世界 X，与 `neg_v` 同开即 180° 旋转（只 pool2 需要）。配错表现为静默镜像。
- `blend_px` 是**物理宽度**不是比例：120px @240ppm = 85px @170ppm = 0.5m。underwater2 相邻块只重叠 121~241px（8.14-02 起 14 对里 13 对正好 0.50m），underwater 重叠 361~1081px，两者同用 120。
- `ppm` 是绝对量，世界尺度变了也不用改它。
- `build_remap` 自己把三角形与画布求交，不靠 `still_margin` 保证不越界：margin=0 的资产画布上 underwater2 顶行实测落在 y=-1，而负切片起点会从对侧边缘取像素，既不报错也画错地方。
- `ref_tex`：underwater 走 `snapshot`，但快照已移入日期层，`frames_for_camera(裸相机名)` 返回空 → 新线用 `video`（片段首帧）。
- 步骤：`extract`（FBX→`outputs/<line>/mesh.json`）、`tex`、`still`（静图+网格诊断+**视野区间图**+融合热图+示意图叠加）、`video`、`asset`（→`build/assets/generated/<line>.swasset`）、`build`、`live`。`extract`/`asset`/`build` 按 mtime/口令跳过，`still`/`video` 每次都渲。
- **一条线只有一份 mesh.json**。`lane_meters=True` 的线（目前 overhead*）在 extract 时顺带写进每块的 `kind` 和每个顶点的 `meter: {x, y}`（规则见下面 fbx_overlay 一节，那是纯模块，import 来的不是复制的）；其余线保持裸几何 `{pos, uv}`，因为它们的网格不是双方约定的标定物，且 mesh.json 是 `.swasset` 逐字节编译的输入——多写键会改 sha256（实测只差头部那 32 字节，几何完全一致）。
- `still` 的产物各答一个问题：`stitch` 成品、`_grid` 几何（只画网格不写字）、`_spans` 每台相机的视野区间（`|--- ---|`：实线独占、虚线过渡、端点视野边界、相邻交替高度、字号统一）、`_heat` 融合权重、`_label` 泳道示意图墨迹叠加（**只有声明了 `label_line` 的线才出**，目前 overhead*）。`_label` 由 `compose.draw_label_line` 把示意图缩放到画布后**只取墨迹**（三通道都 ≥200 视为纸白，透明）画在**未加网格的**成品上——要问的是示意图刻度落不落在实拍泳道上，网格线会和示意图自己的线抢。`--tex-dir` + `--tex-pattern` 可换任意一批每相机贴图（如数据集的 `_background.png`），`--still-suffix` 给产物命名——两批数据同 pattern 会互相覆盖。
- `overhead2` 是 `25 水面.fbx` 的那条线：与 overhead 同一批相机、同一批片段，**渲染口径逐字段相同**（170ppm / 85blend / clip_uv / manifest 同步），所以两条线落在同一张 4255×515 画布上、可逐块比对。模型改动是 2.5m 泳道多了一条中间拉线（200/340 三角 vs 旧 120/204）+ 整体上移 11.47m（纯平移，下游不受影响）。相机身份按贴图像素相关认过：`overhead5_merged.png`→overhead5（corr 0.858，次高 0.372）、`overhead6_merged 拷贝.png`→overhead6（0.843/0.377），与世界 X 排序一致。
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

# 拼接线（六条）
./scripts/run_stitch.sh pool extract,still
./scripts/run_stitch.sh underwater extract,asset,build,live --video-dir /path/to/dir --seconds 12
./scripts/run_stitch.sh overhead tex,still --real --video-dir /path/to/dir
./scripts/run_stitch.sh overhead2 extract,still        # 重建 FBX，同一批片段

# 拼接线（Windows 双击入口；片段目录按机位写在 bat 的 DIR_* 变量里）
scripts\run_win.bat                # 默认 pool2 + d3d11 + 30s
scripts\run_win.bat under2 12      # 换线；over2 / pool / under / over 同理

# 入水检测全流程
./scripts/run_water_entry.sh
./scripts/run_water_entry.sh --skip-predict   # 复用已有预测

# FBX 网格叠加（入水机位两线：water_entry/water_entry2）
./scripts/run_fbx_overlay.sh [输出目录] [--line water_entry|water_entry2 ...]

# 标定数据验收（inputs/ 不在 git，搬运后跑这个）
./scripts/check_inputs.sh                             # 两代
./scripts/check_inputs.sh v2                          # 只校验二代
./scripts/check_inputs.sh --write                     # 重写清单（当前树已知正确时）

# 快照整理/合成/标注/拼接统一入口（水下16 + overhead + femto/gemini + 6cam zcam）
./scripts/run_frames.sh products                      # 四类标定产物一键全出（配方在 PRODUCTS）
./scripts/run_frames.sh products --only sixcam        # 只跑一类；--dry-run 只打印
./scripts/run_frames.sh organize                      # 整理所有相机；水下额外差分筛选
./scripts/run_frames.sh auto_merge --camera underA1   # 自动合成（中值+差分），--camera 必填
./scripts/run_frames.sh merge                         # 手动合成（mask 前景+中值背景）
./scripts/run_frames.sh grid                          # 水下 4×4 拼接（每帧标帧ID+米数）
./scripts/run_frames.sh label                         # 打开浏览器 mask 标注器
```

环境：`.venv`（Python 3.10）+ `requirements-win.txt`（numpy / opencv-python-headless / pillow）+ Autodesk FBX SDK + `requirements-pose.txt`（torch/ultralytics，约 2.5GB，**仅**入水检测链路需要，由 `install.bat pose` 安装）。手工准备检查（两平台同口径）：`python -c "import fbx, numpy, cv2, PIL"`——pillow 是 frames 链路的硬依赖（`labeling/merge_overhead.py` 模块级 import），曾漏在 requirements 外导致 Windows 上该链路直接 ImportError。

## 入水检测链路（water_entry）

单机位 YOLO-pose 难例筛选：`predict.py` → `select_frames.py` → `annotate_preview.py` → `export_package.py`（`scripts/run_water_entry.sh` 顺序调用；`review.py` 是基于已有 per-frame JSON 的独立复核入口）。manifest.csv 是片段唯一来源，后续步骤全量重算、无增量状态。

- 数据集根默认 `/Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-up/swimming-gz-bad`，用 `WATER_ENTRY_DATASET_ROOT` 覆盖；产物默认 `outputs/water_entry/`，用 `WATER_ENTRY_OUTPUT_ROOT` 覆盖。
- 判据集中在 `common.py`：入水帧取 `backstroke.entry_frame`（`res.json`）优先，`manifest.water_frame` 只作对照——`water_frame` 在 `backstroke_applied=False` 时不可信。
- 筛选阈值唯一来源是 `select_frames.py` 的 `DEFAULT_*`，`run_water_entry.sh` 运行时读取而非复制。

## FBX 网格叠加（fbx_overlay）

`scripts/run_fbx_overlay.sh` → `python -m python.fbx_overlay`（`python/fbx_overlay/`）。把入水机位 FBX 的网格画到相机原图上，并把网格线换算成真实米数。**两条线**（旧新并存，同 stitch 的 pool/pool2 模式，`profiles.py` 每线一条记录；加线只加记录）：

| line | 子相机 | 内容 |
|---|---|---|
| `water_entry` | 2 | 旧 005/006 单 mesh（006.fbx/Plane004 垂直 + 005.fbx/Plane005 水面），底图 `background.jpg`（无全屏矩形，用 `base_image_path`） |
| `water_entry2` | 2 | femto + gemini，各含全屏矩形（纹理=相机原图）+ 垂直 + 水面 |

- 每个子相机一个 FBX，自带全屏矩形（纹理=相机原图）作底图；`CameraSpec` 支持一线多 FBX（femto+gemini 同属 water_entry2）。
- **俯视线（overhead*）不在这里**：自上往下看，UV 不映射到任何单张相机帧，它们是 **stitch 的第 5、6 条线**。米数由 `lane_meters` 写进 stitch 那份 `outputs/<line>/mesh.json`（规则仍是本包的 `meters.annotate_meshes`），**一条线只有一份 mesh.json**。曾经在这里重造过一遍 canvas 拼接（`overlay_stitch.py`，stitch 五步的逐行复制）并额外写过一份 `overlay/mesh.json`，两者都已删除。
- **CLI**：`--line`（可重复，默认全部）；`--camera` 线内子相机过滤，`--camera femto|gemini` 无 `--line` 时兼容映射到所属线。`--mesh FBX NODE` 旧回归路径不变。
- 语义分类自动判定（`classify.py`，不硬编码节点名）：tris≤4 且 UV du≈dv≈1 → 全屏矩形；dv≥0.3 → 垂直水面；dv<0.3 且 v_min≥0.2 且 tris≥100 → **plane**（俯视泳道平面）；否则 → 水面（图像底部横带）。阈值是 `classify.py` 顶部常量，数据驱动。
- **网格米数**（`meters.py`，纯模块——无 FBX SDK / OpenCV / NumPy，所以 stitch 也能 import）：两种规则按 kind 分发——
  - vertical/surface：锚点 + 实测 step（"以网格为准"）：右列跳过（右2=0.5m，向左 +step）；垂直跳最下/最上行（下1=0m，向上 +0.25m）；水面 Y 按 gap 聚类成带（下带=0m，向上每带 +实测带距）。
  - plane（overhead*）：**世界差**——X 不跳列，`meter = 泳道最右X - 列X`（两 plane 组合，Plane002 读 15-25m）；Y 跳最下/最上，下1=0m，`meter = y - 下1`（0/0.875/1.625/2.5）。
  - 唯一入口是 `annotate_meshes(meshes)`：就地给每块加 `kind` 字符串、把 triangles 换成带 meter 的副本；有 plane 时按整份 mesh 列表取泳道最右 X。stitch 的 extract 和本包的 `annotate_document` 都走它。
- 产物落 **`outputs/<line>/overlay/<camera>/`**（`overlay/` 是为了不和 water_entry 检测链路的 `predict/review/` 混在同一层）：
  - **`mesh.json`**：完整几何 + kind，**每个 vertex 内联 `meter: {x, y}`**——被跳过的网格线（最下行等）顶点无 meter；算法可做三角形内重心插值；`camera` 字段=线名。
  - `<camera>_mesh_overlay.png` 合成图 + 每 mesh 一图，默认叠加米数标签（**标签颜色 = 所属 mesh 颜色**；X 米数标在网格上方、Y 米数标在图像右侧；超出可视范围的列/行不写），`--no-labels` 关闭但 JSON 仍写。
- 默认 UV V 原点 `bottom`（FBX 惯例，像素 `y=(1-v)*(height-1)`）；模型按图像坐标制作时用 `--uv-v-origin top`。坐标不钳制，越界交给 OpenCV 裁剪。
- 渲染纯函数在 `render.py`（不 import fbx，可无 SDK 测试）；`classify.py`/`meters.py` 也是纯函数。CLI 在 `__main__.py` 负责场景生命周期（`read_scene` 的 `manager.Destroy()`）、分类与产物落盘。
- 本机 FBX SDK 读取会生成 `.mayaSwatches/*.swatch` 缓存文件，属本地产物，不要提交。

## 快照整理/合成/标注/拼接（frames）

`scripts/run_frames.sh` → `python -m python.labeling.frames`（`python/labeling/frames.py`），六条子命令共用同一入口：

- `products`：按 `PRODUCTS` 配方一次跑完四类标定产物（见下），`--only` 挑一类、`--dry-run` 只打印。
- `organize`：所有相机按时间序整理成帧文件夹（`f<NN>_<snapshot>__<orig>.jpg`，字节级拷贝）；水下 16 相机额外做中值背景差分筛选。
- `auto_merge --camera X`：自动合成（中值背景 + 差分前景叠加，分带内存封顶），相机必填。
- `merge`：手动合成，读 `mask_label_project.json`，mask 覆盖处取原帧、其余取中值背景，处理工程里所有相机。
- `grid`：仅水下，16 相机 mask 合成图 4×4 cat 拼接（纯可视化），每格标相机 ID + 泳道米数。
- `label`：起浏览器 mask 标注器（选目录即通用：overhead/underwater/femto/gemini）。

**merge 的融合与标米**：
- `--dates D1 D2 ...`：多数据集融合，同名相机帧按 `--dates` 顺序叠加（后叠在上层）并统一重编号；**只给一个日期时保留工程里的帧号**（那是该相机的全局位置，压成 1..N 会让米数全错）。
- `--meter-spec`：帧→米数口径 json，默认 `<snapshots>/frame_meters.json`；`--meter-overrides '28:14.5'` 临时纠个别帧。
- **标注自动判断**：`underA*`（水下 16 相机）标 `f<帧ID> <米数>`；其余相机（overhead / gemini / femto / orbbec）**完全不标**（`annotate=False`）。

### 4 类标定产物：一条命令出全部

配方写在 `python/labeling/frames.py` 的 `PRODUCTS`（数据即文档），`products` 子命令照它执行，产物名直接是交付名，不需要事后手工改名：

```bash
bash scripts/run_frames.sh products                 # 四类全跑
bash scripts/run_frames.sh products --only sixcam   # 只跑一类
bash scripts/run_frames.sh products --dry-run       # 只打印要跑什么
```

| 类 | 内容 | 产物落点 |
| --- | --- | --- |
| `underwater` | 20260807 水下 16 相机 mask 合成（标 `f<帧ID> <米数>`）+ 4×4 拼接 | `20260807/object-frames/underA*_mask_merged.png` + `underwater_mask_grid.png` |
| `sixcam` | Horizontal+Vertical 横竖合并的 6 相机拉线自动合成（zcam1-4 + overhead5/6） | `20260807-6cam-Horizontal/object-frames/*_merged.png` |
| `overhead` | 20260708 + Horizontal 融合的 overhead5/6 mask 合成 | `20260708/object-frames/overhead5|6_merged.png` |
| `entry` | gemini/femto 各数据集**单独**出（20260708 是旧相机名 `orbbec_camera_1`） | 各数据集 `object-frames/*_merged.png` |

改配方就改 `PRODUCTS`，不要再往文档里抄命令——文档抄漏过一次（overhead5/6 本属 sixcam 批次却被落下）。

前置：水下与入水机位要先用 `label` 画 mask（工程落 `<date>/snapshots/mask_label_project.json`）；`sixcam` 全自动无需 mask。

**关键口径**

- **米数不写死在代码里**：录制事故是这批数据的属性，放 `<snapshots>/frame_meters.json`（`frame-meters/v1`）：`start`/`step` 定等距，`gaps:[n]` 表示第 n 帧后缺一帧（米数跳一格），`skip:[n]` 表示第 n 帧是重复帧（不标、不占位）。文件缺失按 0.5m 等距。20260807 的口径是 `gaps:[28] skip:[35]`。
- **帧号只在跨数据集时重编号**：单数据集时工程里的 `frame_index` 就是该相机在全局时间轴的位置（如 underA11 是 f27~f36），重编号会压成 f01~f10 让米数全错，已加回归测试。
- **自动合成只有一个判据**：与中值背景的 RGB 距离 > `--thresh`（默认 40），按时间序后帧覆盖前帧，与 `merge_overhead` 逐位一致（单测断言）。曾试过用逐像素 MAD 门控滤"每帧都在晃"的水花/灯光反射，实测无效并已删除：水花的 MAD 高（≈34 vs 拉线 ≈3）但偏离幅度也高（113 vs 95），`偏离 > MAD×k` 对两者同时成立——量出来拉线保留率与水花残留率按同比例一起掉（gate=20 时分别 51%/57%），没有哪个 k 只滤水花。zcam 合成里的块状糊主要来自单帧本身的水面状态，要处理得筛形状（拉线是长直线、水花是团块）或在采集侧解决，不是筛时序统计量。
- **内存自适应**：中值背景要整条带装全部帧，帧数多时按 `MEM_BUDGET_BYTES`（512MB）自动收窄带高，不必手动调 `--band-rows`（带高不影响结果，只是分几次算）。
- **产物名**：水下出 `_mask_merged`（`grid` 要读），其余出 `_merged`（交付名），由 `--merged-suffix` 控制。背景统一 `_background`，三条链路内容一致。

- 数据根 `<数据集根>/<date>/snapshots/`，产物统一 `<数据集根>/<date>/object-frames/`（与 20260708 的 object-frames 平级）：每相机一个目录、`detections.csv`（全帧差分统计）、`curated.csv`（is_object=1 的值得标注帧）。
- 差分口径对齐旧 detections.csv：`score_frac_gt40` 是该帧与相机逐像素中值背景的 RGB 欧氏距离 > 40 的像素占比；`cam_median` 是全时段 score 中位数；`threshold = cam_median × 1.28`（对齐旧数据 ~34% 精选率）；`is_object = score > threshold`。
- `snapshots.py` 的 `frames_for_camera(camera, date=None)` 支持日期层：`date` 缺省走旧布局 `<root>/snapshots`，传入则走 `<root>/<date>/snapshots`；frames 的 `frames_for_camera` 是本模块口径（可 patch 测试）。
- 中值/差分按水平条带算，峰值内存 ≈ 帧数 × 带高 × 宽 × 3，`--band-rows` 调小可压内存。

## 编码风格与提交

- Python：4 空格、小写下划线，模块入口一律 `python -m python.<pkg>[.<mod>]`。C++20：`swim::core` / `swim::d3d11` / `swim::cudagl`，成员变量带尾下划线。
- 源码 UTF-8；注释解释**为什么**，尤其是「看起来可以简化但实际不能」的地方。
- `scripts/**/*.bat` 必须 UTF-8 无 BOM + CRLF，第三行 `goto :run`，中文只放在被 goto 跳过的说明区，执行区注释一律 ASCII；改动后运行 `scripts\checks\check_bat_format.ps1`。
- **Windows 上跑通的四条踩坑口径**（都已修在源码里，遇到同类症状先想这四条）：
  - 图像 IO 一律走 `python.common.media` 的 `read_image`/`write_image`（内部 `imdecode`/`imencode` + Python 开文件）。`cv2.imread`/`imwrite` 在中文机器上按 ANSI 解路径，`25 水面.fbm` 这种路径会报「can't open/read file」，看着像文件缺失。
  - `.sh` 里选解释器要先试 `.venv/Scripts/python.exe` 再 fallback：Windows 的 `python3` 是 WindowsApps 别名，会弹应用商店并以 49 退出、**什么都不打印**。
  - 测试里读带中文的源码/脚本必须显式 `encoding="utf-8"`，裸 `open()` 按 GBK 解会 UnicodeDecodeError。
  - `shasum`/`sha256sum` 传 Windows 路径时按 GNU 惯例转义反斜杠、整行前加 `\`，`awk '{print $1}'` 会取到带前导反斜杠的哈希；改成从 stdin 读。
- 提交信息 `type(scope): 简短祈使句`，与现有历史一致。
- 不提交：`build/`、`outputs/`、`third_party/`、`*.pt`、大视频、`.swasset`、`.venv`、**`inputs/` 除 `configs/` 外的全部**（见开头「标定数据不在 git 里」）。`outputs/pool/mesh.json` 被 gitignore 却是 CMake 硬依赖（缺了任何 target 都编不过），用 `./scripts/run_stitch.sh pool extract` 生成。
