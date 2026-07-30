---
title: 俯视水道拼接（overhead lane stitch）— 002.fbx 两路变体与 stitch profile 抽象
date: 2026-07-30
status: draft
---

# 俯视水道拼接设计文档

## 背景

仓库目前有三条互不依赖的相机任务线：

| 线路 | 代码 | 拓扑 | 产物 |
| --- | --- | --- | --- |
| pool 六路 4K | `python/assets/extract_fbx.py` + `python/validation/reference_renderer.py` | 6 块平面**两排**，相机序 `cam3 cam2 cam1 cam4 cam5 cam6` | `pool_4k.swasset`、`outputs/images/`、`outputs/videos/` |
| underwater 16 路 | `python/underwater/` | 16 块平面**一字横排**，world-X 升序即左→右 | `underwater_16.swasset`、`outputs/underwater/` |
| water entry 单路 | `python/water_entry/` | 非拼接（YOLO-pose 姿态识别） | `outputs/water_entry/` |

设计师新下发 `inputs/002.fbx`：两块俯视平面，提取出**一条水道**，目的是让水上视角与
水下 16 路关注同一名运动员。

## 实测事实

以下全部已在本机验证，不是推断。

### 002.fbx 与水下 16 平面同构

```
Plane002  tris=120  tex=05-02.jpg  x[-35.22,-25.22]  const_axis=2  kept=[0,1]
Plane001  tris=204  tex=C06.jpg    x[-27.72,-10.22]  const_axis=2  kept=[0,1]
```

| 维度 | 002.fbx | all.fbx（水下 16） |
| --- | --- | --- |
| 世界 X 跨度 | 25.000000 m | 25.000001 m |
| 世界 Y 跨度 | 3.000000 m | 3.000000 m |
| 网格数 | 2 | 16 |
| 常量轴 | Z（`kept=[0,1]`） | Y（`kept=[0,2]`） |
| 杂物网格 | 无 | 有（支架框、泳道条、重复网格） |
| 源尺寸 | 3840×2160 | 1280×720 |

同一条 25 m 水道的两侧视角。`python.underwater.extract` + `render` 原样喂 002.fbx
一次跑通，输出 `canvas 6005x725 @ 240px/m`，无需改动任何几何代码。

`const_axis` 不同不影响任何下游逻辑：`detect_constant_axis` 逐 FBX 自行判定，
`pos[0]`/`pos[1]` 始终是保留下来的两个轴，渲染只认这两维。

### 贴图 ↔ 相机对应已量化确认

`002.fbm/` 的两张贴图是带黄色标定线的 3840×2160 帧。用 SIFT + RANSAC 与
`20260629-4K` 的 cam5/cam6 首帧配准：

| 贴图 | 对应视频 | 良好匹配 | RANSAC 内点 | 单应四角位移 |
| --- | --- | ---: | ---: | --- |
| `05-02.jpg`（overhead5） | `20260629_172532_cam5.mp4` | 292 | 99 | 均值 2.9 px |
| `C06.jpg`（overhead6） | `20260629_172532_cam6.mp4` | 477 | 187 | 均值 0.3 px |

单应矩阵近似恒等（对角 0.9996~1.002，平移 <0.8 px），确认为同机位同标定。
反向交换贴图后重渲，拼接图立即出现肉眼可见错位 —— 该对应是硬约束。

**结论**：设计师的 `overhead5`/`overhead6` 机位命名与 4K 数据集的 `cam5`/`cam6`
指向同一对物理相机。

### overhead5/6 无原始视频

全数据集扫描 `*overhead5*` / `*overhead6*`：仅命中
`swimming-xlj-middle-20260708/snapshots/raw_*/` 下 50 组 jpg，`.ts`/`.mp4` 零命中。
`水上检测/20260713/*_overhead.mp4` 是 3054×360 的**已拼接**流，不是原始机位。

因此本轮视频侧使用 `20260629-4K` 的 cam5/cam6（已确认同机位）。

### 4K 数据集无墙钟对齐字段

`20260629_172532_manifest.json`：`files[]` 只有 `name`/`path`；
`sync_summary.status = "waiting_for_syncbridge_events"`、`ptp_lock_verified = false`；
`sync_map.json` 的 `mappings[].offset_us` 全为 `null`、`mapping_status = "pending"`。

六路 ZCAM E2N 同处一个 EzLink/IEEE1588 同步域、同一次录制会话（`session_id`
`20260629_172532`，`state: completed`），偏差在帧级。**没有可用的墙钟锚点，也不需要。**

### 源图原生像素密度

逐三角反解 UV↔世界的仿射雅可比，量出每块平面的源像素密度：

| 线路 | 水平 | 垂直 | 现用 / 拟用 ppm |
| --- | --- | --- | --- |
| overhead 2 路 4K | 152.5~161.3 px/m | 165.4~168.7 px/m | **170** |
| underwater 16 路 720p | 234.2~259.4 px/m | 240.0~256.4 px/m | 240（不变） |
| pool 6 路（若源 4K） | 158.4~184.1 px/m | 119.6~135.6 px/m | 100（不变） |

## 目标与范围

**本轮做**：

1. `python/underwater/` → `python/stitch/`，差异收进 `profiles.py`。
2. `overhead` profile：002.fbx 两路，离线（静图 / 网格图 / 热图 / mp4）+ 实时
   （`.swasset` + Metal 跑通）。
3. 步骤 dispatcher 与单一 shell 入口，退役 `uw-*` 与 `run_underwater.*`。
4. `inputs/002.fbx` 落位到 `inputs/overhead/models/`。
5. 单测与端到端验证。

**本轮不做**：

- 不把 pool 六路纳入 profile 注册表（理由见「能力边界」）。
- 不接 overhead5/6 的真实 `.ts`（数据不存在）。
- 不做跨线路的时间同步（水上水下同一运动员的**画面**对齐是下一个任务）。
- 不改任何 C++ 代码。

## 架构

### profile：一条拼接线路的全部差异

```python
@dataclass(frozen=True)
class Profile:
    name: str                      # "underwater" / "overhead"
    fbx: Path
    tex_dir: Path                  # FBX 内嵌绝对路径失效后 SDK 回退的 .fbm
    still_tex_dir: Path            # 静图默认贴图源（见下）
    camera_ids: tuple[str, ...]    # 左→右，与 world-X 升序的 mesh 一一对应
    clip_suffix: str               # ".ts" / ".mp4"
    ppm: float                     # .swasset 画布口径；静图在 full_res=False 时也用它
    full_res: bool                 # 静图是否缩放回源图高度
    blend_px: float
    clip_uv: bool                  # 排除 UV 落在源图外的像素
    crop_bottom: str               # "auto" / "none" / "N"
    planes_only: bool              # all.fbx 需滤杂物；002.fbx 不需要
    sync: str                      # "manifest" / "none"
    source_size: tuple[int, int]   # (1280,720) / (3840,2160)
    ref_tex: str                   # 参考贴图来源："snapshot" / "video"
    out_dir: Path
    asset: Path
```

两条记录：

| 字段 | underwater | overhead |
| --- | --- | --- |
| `fbx` | `inputs/underwater/models/all.fbx` | `inputs/overhead/models/002.fbx` |
| `tex_dir` | `.../all.fbm` | `inputs/overhead/models/002.fbm` |
| `still_tex_dir` | `$DATASET/annotation-grids`（env 可覆盖） | `inputs/overhead/models/002.fbm` |
| `camera_ids` | `underA16` … `underA1`（16） | `("cam5", "cam6")` |
| `clip_suffix` | `.ts` | `.mp4` |
| `ppm` | 240.0 | 170.0 |
| `full_res` | True | False |
| `blend_px` | 120.0 | 85.0 |
| `clip_uv` | True | True |
| `crop_bottom` | `"auto"` | `"none"` |
| `planes_only` | True | False |
| `sync` | `"manifest"` | `"none"` |
| `source_size` | (1280, 720) | (3840, 2160) |
| `ref_tex` | `"snapshot"` | `"video"` |
| `out_dir` | `outputs/underwater/` | `outputs/overhead/` |
| `asset` | `build/assets/generated/underwater.swasset` | `.../overhead.swasset` |

`camera_ids` 顺序即 world-X 升序，与 `extract` 的排序、`compile_runtime_asset` 的
按位置 `zip` 三处口径统一。

`clip_uv` / `crop_bottom` 的两条取值都对齐 `run.py` 现有默认（`--no-clip-uv` 反向开关
默认 True、`--crop-bottom auto`），overhead 只把 `crop_bottom` 改成 `"none"`（理由见
「几何参数依据」）。

两个易混点在 profile 里被显式区分，而不是像现在那样散在 shell 变量与 CLI 默认值里：

**`tex_dir` ≠ `still_tex_dir`**。前者是 FBX 提取时解析贴图 basename 的目录（`.fbm`）；
后者是渲静图实际读的图。水下这两者不同 —— `run_python.sh:161-162` 用数据集的
`annotation-grids/`（权威网格渲染）而非烘进 `all.fbm` 的那份，注释也写明了。overhead
两者同为 `002.fbm`（设计师的标定图就在里面）。

**`full_res` 与 `ppm` 并存**。水下静图走 `--full-res`（缩回源高 360，产物 3278×360）、
而 `.swasset` 用 ppm=240（画布 6005×725）—— 两个口径本就不同，前者给人看、后者给 GPU。
overhead 两者统一为 170，`full_res=False`。profile 把这个差异记下来，不再靠调用方
记得给哪个 CLI 传 `--full-res`。

### 目录改名

`git mv python/underwater python/stitch`（保留文件历史）。目录名当前在撒谎 —— 它即将
装着俯视相机的线路。模块名保持不变：

```
python/stitch/
├── __init__.py
├── profiles.py        # 新增：Profile 数据类 + 注册表 + get()
├── __main__.py        # 新增：步骤 dispatcher
├── extract.py         # 不变
├── render.py          # 不变（几何/权重/裁剪全部通用）
├── render_video.py    # 改：删 camera_of()，加 sync 分支
├── export_ref_tex.py  # 原 export_real_tex.py，泛化到两种来源
└── run.py             # 改：模块常量 → profile
```

### 删除写死水下的三处

**`render_video.camera_of()`**（`render_video.py:44`）：正则 `(underA\d+)` 从贴图名
反解相机，对 `C06.jpg` 返回 `None` 并 fatal。整个删掉 —— profile 已声明有序
`camera_ids`，`extract` 已按 world-X 排好序，按位置 `zip` 并校验数量即可。少一个正则、
少一类「贴图改名就崩」的失败模式。

**`run.py` 顶部模块常量**（`run.py:29-38`）：`MESH_JSON`/`ASSET`/`ASSET_STAMP`/
`CAMERA_IDS`/`ASSET_PPM`/`ASSET_BLEND_PX`/`SOURCE_SIZE`，以及 `write_config` 里的
`*_{camera}.ts` glob（`run.py:200`）与 `lane_start_offsets` 里的 `CAMERA_IDS` 过滤
（`run.py:175`）。全部改为从 profile 取。

`ASSET_STAMP` 机制保留 —— 它记录 `.swasset` 是用哪组 shaping 参数烘的，改了参数即便
mesh JSON 未动也会重编。profile 名一并进 stamp 字符串，避免两条线路的 stamp 互相误判。

**`render_video` 无条件要求 manifest**（`render_video.py:60-69`）：改为按
`profile.sync` 分支。`sync="manifest"` 保持现有行为，包括缺 manifest 即 fatal —— 那条
线路的各路偏差达数秒，静默退化是错的。`sync="none"` 各路从第 0 帧读，不读 manifest。
CLI `--no-align` 保留为显式覆盖开关。

实时侧的 `lane_start_offsets`（`run.py:159-189`）已经是软失败：`load_manifest` 抛
`SystemExit` 时它打印一行并返回 `{}`。`sync="none"` 时直接跳过调用，连那行提示也不打 ——
「这条线路本来就没有 manifest」不该每次都报告成异常。

### 步骤 dispatcher

```
python -m python.stitch <profile> <step>[,<step>…] [选项]
```

| 步骤 | 底层 | 产物 |
| --- | --- | --- |
| `extract` | `stitch.extract` | `<out_dir>/mesh.json` |
| `tex` | `stitch.export_ref_tex` | `<out_dir>/ref_tex/` |
| `still` | `stitch.render` | `<out_dir>/stitch[_real].png`、`grid[_real].png`、`heat[_real].png` |
| `video` | `stitch.render_video` | `<out_dir>/stitch.mp4` |
| `asset` | `assets.compile_runtime_asset` | `profile.asset` |
| `build` | cmake | `swim_realtime` |
| `live` | `swim_realtime` | 预览 / HEVC / metrics |

产物名不再编入 `bp<N>` 后缀（现状 `all_stitch_bp120.png`）。blend 是 profile 的既定
参数，不是每次要对比的变量；需要横向对比时显式传 `--still <path>` 到低层 CLI。

现有 `run.py --steps extract,asset,build,run` 就是这个机制，只是步骤表停在实时四步，
`still`/`video`/`tex` 另有各自 CLI 和各自 shell 包装。合并成一张表后，`newer_than`
的「产物比输入新就跳过」对离线步骤同样生效 —— 现在 `uw-render` 每次都重算。

各模块自己的 argparse 全部保留：那是低层显式接口（`python -m python.stitch.render
--ppm 313 --crop-bottom-px 40` 这种一次性口径），单测也直接调函数。dispatcher 只做
一件事 —— 拿 profile 的默认值把它们填满。

`still` 加 `--real` 开关选参考贴图：默认用 `profile.tex_dir` 里的标定图，`--real` 用
`<out_dir>/ref_tex/`，产物加 `_real` 后缀以免两者互相覆盖。对应现在的 `uw-render` /
`uw-real`。

`tex` 步骤按 `profile.ref_tex` 取源：`"snapshot"` 走 `annotation_preview.common` 的
数据集快照索引（水下现状），`"video"` 从 `--video-dir` 下每路片段读第 0 帧，因此
overhead 跑 `tex` 必须给 `--video-dir`，缺失即报错。

`extract` 产物统一为 `<out_dir>/mesh.json`（现状 `all_mesh.json`）。这会使已有的
`outputs/underwater/all_mesh.json` 不再被 `newer_than` 认作当前产物，首次运行会重跑
一次提取 —— 一次性代价，`outputs/` 本就不入库。

### shell 入口收敛

`scripts/run_stitch.sh` + `scripts/run_stitch.ps1`，逐字转发到
`python -m python.stitch`。

同时修掉 `run_underwater.ps1` 的实际债务：它有 12 项 `param()` 块，把 `run.py` 的
argparse 抄了一遍（`-Seconds`/`-Encode`/`-NoWindow`/…），加任何新参数都得改两处。
改成 `@args` 直接转发即消失，与 `.sh` 做法一致。

退役 `run_python.sh` 的 `uw-*` 五条、`scripts/run_underwater.sh`、
`scripts/run_underwater.ps1`。迁移一对一：

```
uw-extract              →  run_stitch.sh underwater extract
uw-tex                  →  run_stitch.sh underwater tex
uw-render 120           →  run_stitch.sh underwater still --blend-px 120
uw-real 120             →  run_stitch.sh underwater still --real --blend-px 120
uw-video DIR 120        →  run_stitch.sh underwater video --video-dir DIR --blend-px 120
run_underwater.sh DIR   →  run_stitch.sh underwater extract,asset,build,live --video-dir DIR
```

`we-*`（入水检测）与 pool 的 `still`/`4k`/`extract`/`asset`/`keypoint`/`oh-merge`/
`label` 留在 `run_python.sh` 不动 —— 它们不是拼接线路。

注意 `run_python.sh` 已有的 `oh-merge` 指的是 annotation_preview 的 overhead 快照
时序合并，与本文档的 overhead 拼接线路是不同任务，不复用该前缀。

### README

「水下拼接」一节（README:376-459）改写为「平面拼接（stitch）」，两条 profile 并列成
表，命令换成 `run_stitch.sh`。目录结构树（README:176-183）去掉两个
`run_underwater.*`、加 `run_stitch.*`。README 现有 20 处 `python.underwater.*` /
`underwater_16.swasset` / `all_mesh.json` 引用一并更新。

### 能力边界：pool 不进 profile 注册表

看着该合并，其实不能。pool 六路的网格是**两排**（`01/02/03` 一排、`u/Plane004/
Plane007` 另一排，y 分两带），相机顺序 `cam3 cam2 cam1 cam4 cam5 cam6` 不是 world-X
升序；它还要 `neg_v=True` 和距离变换羽化（`feather_weights`），而非竖直硬缝
（`seam_weights`）。

profile 描述的是「N 块平面横向一字排开」这一种拓扑：world-X 升序即左右序、竖直硬缝、
`neg_v=False`。underwater 16 块和 overhead 2 块都在这个类里，pool 不在。把 pool 塞进来
会让 profile 长出 `layout`/`weight_mode`/`order_mode` 三个只为一条线路存在的字段 ——
那是抽象漏了。pool 继续走 `reference_renderer` + `CMakeLists.txt:25-38` 的
`pool_4k.swasset` 规则。

### 资产命名

`underwater_16.swasset` → `underwater.swasset`，新增 `overhead.swasset`。数量是
profile 数据，不该编进文件名。config 每次运行重写，改名无迁移成本。

### FBX 落位与 .gitignore

`inputs/002.fbx` + `inputs/002.fbm/` → `inputs/overhead/models/002.fbx` +
`inputs/overhead/models/002.fbm/`，对齐 `inputs/pool/models/` 与
`inputs/underwater/models/` 的既有布局。

`.gitignore` 三处配套改动：

1. 现有 `inputs/*.fbx` 与 `inputs/*.fbm/`（第 27-28 行）当前正忽略着
   `inputs/002.fbx`。移入 `inputs/overhead/models/` 后 glob 不再匹配（`*` 不跨层级），
   该 7 MB 重资产会变成可提交状态。按仓库既有惯例（`inputs/underwater/models/` 整目录
   忽略、`inputs/pool/models/*.fbx` 忽略）新增 `inputs/overhead/models/`。
   第 27-28 行的散落 fbx 规则保留 —— 它挡的是下次直接丢进 `inputs/` 根的新文件。
2. `inputs/configs/underwater_16_*.conf` → `inputs/configs/underwater_*.conf` 与
   `inputs/configs/overhead_*.conf`（config 是每机器生成物）。
3. 注释里的 `python.underwater.run` 改为 `python.stitch`。

## 数据流

```
inputs/overhead/models/002.fbx + 002.fbm/{05-02,C06}.jpg
   │  extract（world-X 升序 → Plane002, Plane001）
   ▼
outputs/overhead/mesh.json
   │
   ├─ still  ─────────────────────────────→ stitch.png / grid.png / heat.png
   │          （ppm=170, blend=85, 4255×515）
   │
   ├─ tex ── 20260629-4K cam5/cam6 首帧 ──→ outputs/overhead/ref_tex/
   │
   ├─ video ─ 20260629-4K cam5/cam6 mp4 ──→ stitch.mp4（sync=none）
   │
   └─ asset ──────────────────────────────→ build/assets/generated/overhead.swasset
              （--camera-ids cam5 cam6 --ppm 170 --no-neg-v --blend-px 85）
                        │
                        ▼  build + live
              inputs/configs/overhead_metal.conf
              （source.cam5=… / source.cam6=…）
                        │
                        ▼
              swim_realtime --backend metal（预览 / HEVC / metrics）
```

## 几何参数依据

**`ppm=170`**：贴原生密度上界 168.7 px/m，不放大、不丢细节。画布 4255×515。

**不走 `--full-res`、`crop_bottom="none"`**：`full_res=False` 时声明的 `ppm` 直接生效，
不再自适应。实测 `bottom_dirty_rows=2`，而顶部同样是 2 —— 那是 `render.py` 的
`margin=2` padding 残留，不是真缺口。水下的 67~68 行来自矮平面的透视地面缺口，
overhead 两块都是全高平面，没有这个问题。

**`blend_px=85`**：两块在 `cols[1277..1702]` 重叠 425 px（2.5 m）。85 px @170ppm
= 0.5 m，与水下 120 px @240ppm 同物理宽度，重叠区容得下。硬缝 `bp0` 目视已无错位，
羽化只消掉一条可见亮度台阶。

**`neg_v=False`**：已渲图确认 002 在此口径下正立（池壁在下、跳台在右），与水下同口径。
pool 的 `neg_v=True` 不适用。

## 错误处理

沿用现有 `SystemExit` / `StepError` 风格：

- profile 名未注册 → 列出已注册名后退出。
- `camera_ids` 数量与 mesh 数不符 → 报出两个数字。这是 `compile_runtime_asset:125`
  已有的校验，dispatcher 在更早的 extract 后即校验，失败得更早。
- 某相机在 `--video-dir` 下无匹配片段或多个匹配 → 分别报错（沿用
  `run.py:149-155` 与 `render_video.video_for_camera` 的现有行为）。
- `sync="manifest"` 且 manifest 缺失或无 align 窗口 → fatal，提示可用 `--no-align`。
- `sync="none"` 不读 manifest，不报错。
- FBX / 贴图目录 / mesh JSON 不存在 → 现有报错不变。

## 测试

新增 `tests/python/test_stitch.py`（原 `test_underwater.py` 改名，import 改
`python.stitch.*`，现有 33 个用例全部保留、断言不动）。新增用例：

1. profile 查表命中两条记录、未知名报错且错误信息列出已注册名。
2. `camera_ids` 数量与 mesh 数不符时报错，且错误信息含两个数字。
3. `sync="none"` 时 `render_video` 不读 manifest（monkeypatch `load_manifest`
   使其一旦被调用即失败）。
4. `sync="manifest"` 时缺 manifest 仍 fatal（守住现有行为不被 profile 改动破坏）。
5. `write_config` 对 `clip_suffix=".mp4"` 的 profile 生成 2 条 `source.cam5=`/
   `source.cam6=`，顺序与 `camera_ids` 一致。
6. 002.fbx 端到端 extract：恰好 2 块、顺序 `Plane002`→`Plane001`、贴图
   `05-02.jpg`→`C06.jpg`（无 FBX SDK 时 `skipUnless` 跳过）。
7. dispatcher 的步骤名解析：未知步骤报错并列出合法步骤。

## 验证

按顺序执行，每步都要看到预期数值：

1. `unittest discover -s tests/python -t .` — 现有 180 个用例（其中 stitch 33 个）
   加新增用例全绿。
2. `run_stitch.sh underwater still --real --blend-px 120` — 与
   `outputs/underwater/all_real_stitch_bp120.png` 逐像素一致（改名与 profile 化
   不得改变水下产物）。用一次性 `cv2.imread` + `np.array_equal` 断言，不用
   `python.validation.compare_images` —— 那个模块的画布尺寸与 `LOCAL_REGIONS`
   全部硬编码为 pool 的 5001×2101，对 3278×360 会直接越界。
3. `run_stitch.sh overhead extract` — 2 块，顺序 `Plane002`(05-02) → `Plane001`(C06)。
4. `run_stitch.sh overhead still` — `canvas 4255x515 @ 170.00px/m`，目视接缝对齐。
5. `run_stitch.sh overhead tex` + `still --real` — 换成 cam5/cam6 真实首帧后接缝
   仍对齐（已在探索阶段用 `/tmp` 冒烟验证过）。
6. `run_stitch.sh overhead video --video-dir <4K> --seconds 10` — 10 s mp4，
   日志出现 `NO time alignment`。
7. `run_stitch.sh overhead asset` — 报出 `2 cameras, canvas 4255x515`。
8. `run_stitch.sh overhead extract,asset,build,live --video-dir <4K> --seconds 10`
   — Metal 实时跑通，记录解码 / 渲染 / 预览 FPS。
9. pool 回归：`run_python.sh still` 与 `asset` 不受影响。

临时产物（`/tmp/probe002/`）测试后清理。

## 后续扩展成本

拿到 overhead5/6 真实 `.ts` 后，改动是 `profiles.py` 里一条记录：同一个 `002.fbx`、
`camera_ids=("overhead5","overhead6")`、`clip_suffix=".ts"`、`sync="manifest"`、
`source_size` 按实际流分辨率。shell、dispatcher、C++ 全不动。

C++ 侧本轮零改动的依据：`kMaxCameras=16`（`camera_capacity.hpp:14`）容得下 2 路；
相机身份本来就取自 config 的 `source.<id>` 声明顺序（`config.hpp:52-57` 注释已明确
「camera identity is data, not code」）。资产改名也无需动 C++ —— 测试 fixture
`tests/fixtures/cpp/underwater.conf:4` 引的已经是 `assets/underwater.swasset`。
