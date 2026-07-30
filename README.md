# swim_fbx_demo

把泳池 / 泳道的 FBX 平面网格与 UV 编译成 GPU 资产，再把多路相机实时拼成一张全景图；
附带三类标注与评测工具。C++20 实时核心 + Python 离线资产链，三个原生后端
（Metal / D3D11 / CUDA-GL）共用同一份 `swim_core` 逻辑。

## 这个仓库有四条互不交叉的链路

要改哪一条，只需要看它那一行；四条链路除了共用 `python/common/` 与 `.venv`，没有其他耦合。

| 链路 | 做什么 | 入口 | 代码 |
| --- | --- | --- | --- |
| **相机拼接** | FBX → 网格 → 静图 / 视频 / `.swasset` → 实时拼接 | `scripts/run_stitch.sh LINE STEPS` | `python/stitch/`、`cpp/` |
| **入水检测** | 单机位 YOLO-pose 预测、难例筛选、送标数据包 | `scripts/run_water_entry.sh` | `python/water_entry/` |
| **数据集标注** | 浏览器标注器、快照合成、关键点复核页 | `scripts/run_label.sh` | `python/labeling/`、`python/keypoints/` |
| **性能取证** | 48 格性能矩阵、十分钟 soak | `scripts/run_bench.sh` | `python/benchmarks/` |

Windows 双击入口 `scripts\run_win.bat` 走的就是第一条链路。每个脚本不带参数运行会打印
自己的完整用法，不必先读本文。

本文所有命令都假定当前目录是仓库根目录。

## 相机拼接

一套代码服务三条相机线，差异全部是 `python/stitch/profiles.py` 里的数据：

| | pool | underwater | overhead |
| --- | --- | --- | --- |
| 机位 | 六路 4K 俯视泳池 | 水下 16 块平面 | 水上 2 块平面 |
| 视角 | 50m 泳池整体，两排机位 | 一条泳道，自下往上 | 同一条泳道，自上往下 |
| 模型 | `pool.fbx`（6 块） | `all.fbx`（16 块 + 杂物需过滤） | `002.fbx`（2 块，干净） |
| 相机 | `cam3 cam2 cam1 cam4 cam5 cam6` | `underA16` … `underA1` | `overhead5`、`overhead6` |
| 网格顺序 | FBX 声明序（两排，不能按 X 排） | 世界 X 升序 | 世界 X 升序 |
| 片段 | `*_camN.mp4` | `*_underAi.ts` | `*_overheadN.ts` |
| 源尺寸 | 3840×2160 | 1280×720 | 3840×2160 |
| 每米像素 | 100 | 240 | 170 |
| 融合方式 | 距离变换羽化 | 竖直硬缝 + 120px 过渡 | 竖直硬缝 + 85px 过渡 |
| 时间对齐 | 无 manifest，按 t=0 | manifest 墙钟 | manifest 墙钟 |
| 资产画布 | 5001×2101 | 6001×656 | 4251×511 |

### 七个步骤

```bash
./scripts/run_stitch.sh LINE STEPS [选项…]        # macOS / Linux
pwsh scripts/run_stitch.ps1 LINE STEPS [选项…]    # Windows
```

`STEPS` 逗号分隔、按给出顺序执行：

| 步骤 | 作用 | 产物 |
| --- | --- | --- |
| `extract` | 读 FBX，提取三角形 + UV，按线路的顺序排列 | `outputs/<line>/mesh.json` |
| `tex` | 导出每台相机的参考贴图（首帧，无标定线） | `outputs/<line>/ref_tex/<camera>.png` |
| `still` | 静图 + 网格诊断图 + 融合热图 | `outputs/<line>/stitch{,_grid,_heat}.png` |
| `video` | 每路片段逐帧拼接 | `outputs/<line>/stitch.mp4` |
| `asset` | 网格 JSON 编译成 GPU 资产 | `build/assets/generated/<line>.swasset` |
| `build` | 构建 `swim_realtime` | `build/<backend>-release/` |
| `live` | 实时拼接（预览窗口 / HEVC / 指标） | `outputs/<line>/realtime.jsonl` |

```bash
# 泳池六路：静图与实时
./scripts/run_stitch.sh pool extract,still
./scripts/run_stitch.sh pool extract,asset,build,live --seconds 30

# 水下 16 路：一条命令跑完提取 → 编译 → 构建 → 实时
./scripts/run_stitch.sh underwater extract,asset,build,live \
  --video-dir /path/to/swb_20260728-150356_6 --seconds 12 --encode

# 俯视两路：设计师标定图的静图，与真实首帧的静图
./scripts/run_stitch.sh overhead extract,still
./scripts/run_stitch.sh overhead tex,still --real --video-dir /path/to/swb_20260730-161710_7
```

常用选项：`--seconds N`（live 时长）、`--seconds-float N`（video 时长）、`--encode`、
`--no-window`（离屏）、`--fps N`、`--blend-px N`、`--ppm N`、`--real`、`--force`、
`--no-loop`、`--config PATH`（用现成 config）、`--backend metal|d3d11|cudagl`。
完整清单：`python -m python.stitch --help`。

`pool` 的片段目录默认取 `SWIMMING_DATASET_DIR`（一个机器级会话目录）；另两条线是
按次挑选的采样目录，必须显式给 `--video-dir`。

`extract` / `tex` / `asset` 的产物比输入新时跳过（`asset` 另比对一份记录了 ppm /
blend / crop 的 stamp，因为 mtime 看不见这些口径变化），`--force` 强制重做；
`still` / `video` 每次都渲 —— 它们的口径可从命令行覆盖，按 mtime 跳过会让人看到
一张过期却像是新的图。

### 平台差异

全部由 Python 处理：macOS 用 Ninja + `metal` 后端 + `build/metal-release/swim_realtime`；
Windows 用 Visual Studio 17 2022 (x64) + `d3d11` 后端 +
`build/win-d3d11/Release/swim_realtime.exe`（CUDA + FFmpeg + GLFW 齐备时可
`--backend cudagl`）。每个后端一棵独立构建树，`build` 步骤在 Windows 上会把
FFmpeg / GLFW / cudart 的 DLL 拷到 exe 旁 —— CMake 不做这件事，缺了会以
`0xC0000135` 启动失败。

运行时 config 每次按片段目录重新生成到 `inputs/configs/<line>_<backend>.conf`，
`source.<camera>=` 的**声明顺序即通道顺序**，C++ 侧直接照此取相机身份。

### 几个不能想当然的地方

**相机身份是位置对应的**：profile 的 `camera_ids` 按顺序配 mesh，不解析贴图文件名
（overhead 的 `05-02.jpg` / `C06.jpg` 无从解析出 `overhead5` / `overhead6`）。改动
FBX 里平面的相对位置、或改 profile 的 id 顺序，会把相机错配到别的平面上，症状是
接缝处错位而不是报错。

**pool 必须保持 FBX 声明顺序**：它的六块网格是两排（`01/02/03` 一排、
`u/Plane004/Plane007` 另一排），按世界 X 升序会把两排交错，让每台相机配到对面那排
的平面上。另两条线是单排，所以按 X 排序。

**pool 用羽化、平面线用硬缝**：pool 两排大面积斜向重叠，没有单一的接缝方向可选，
所以按距离变换羽化，并且**不能**在图像边缘裁剪 UV（会把羽化切掉）。平面线相邻块只
在一条竖直缝相接，所以在有界过渡带内融合并裁剪 UV —— 否则越界 UV 会被 GPU 镜像
采样，正好在缝上画出一条假的条带。

**`all.fbx` 需要过滤，`002.fbx` 不能过滤**：前者夹带无纹理支架、泳道标记条与重复网格，
所以 underwater 开 `planes_only`（只留「每个纹理一块、位于泳池 Y 带内的全高平面」）；
后者恰好只有两块平面，且跨世界 Y `[20.47, 23.47]`（机位在池上方），不在判据的泳池
Y 带 `(-11.6, -8.0)` 内 —— 对它开这个开关会滤掉全部两块。

**`.ts` 第 0 帧不是同一时刻**：录制器把关键帧放在 lookback 窗口内的任意位置，GOP 粒度
使各路偏差可达数秒。两条平面线都声明 `sync="manifest"`：按 manifest 的 `align_start_ms`
与各路 `keyframe_timestamp_ms` 换算每路起始帧，与前端播放器同一套公式。实时侧把同一批
偏移写成 config 的 `source.<camera>.start_ms=`。确实要「各路都从第 0 帧读」时加
`--no-align`。pool 的会话 manifest 不带 align 窗口，本就按 t=0 处理。

**回卷周期取各路可用跨度的最小值**：片段短于运行时长时各路回卷重播而不是走黑帧替换。
若各自在自己的文件末尾回卷，各路会在每一轮上累积几十毫秒的漂移。`--no-loop` 恢复
「播完即止」。

**静图画布比资产画布各维大 4px**（pool 除外）：`still` 给世界边界加了 2px padding 防
浮点舍入越界，资产编译不加。pool 的 margin 是 0，因为它的画布尺寸就是发布尺寸。

**`--full-res`（underwater 默认开）**输出高度对齐源图高度、宽度等比缩放；缩放前会自动
砍掉最下方存在无纹理像素的整行（矮平面的透视地面缺口），再等比缩放。pool 与 overhead
的 `ppm` 已是原生密度，所以关掉。

**overhead 的两张贴图** `05-02.jpg` / `C06.jpg` 是设计师在两个机位标定的帧。用 SIFT +
RANSAC 与实际片段首帧配准，内点 68 / 166、单应近似恒等（四角位移均值 4.2px / 0.6px），
确认为同一对物理相机。`C06.jpg` 缺一条标定线，`python -m python.stitch.patch_grid
overhead` 会把它按 FBX 自己的 UV 推导出来补画进去（`--dry-run` 只报位置不落笔）。

### macOS/Metal 实测

三条线各跑 6 秒离屏，`decoded_pixel_host_copies=0`、`pool_exhaustion=0`、`malformed=0`：

| 线路 | 输出 | 渲染 | 单路解码 |
| --- | --- | ---: | ---: |
| pool | 5002×2102 | 30.1 fps | 30.2 fps |
| underwater | 6002×656 | 30.1 fps | 80.4 fps |
| overhead | 4252×512 | 30.1 fps | 10.1 fps |

overhead 单路解码明显低是像素总量所致（两路 4K 约为 16 路 720p 的 2.4 倍），渲染不受影响。

## 入水检测机位

`python/water_entry/` 是第四类相机：水下 0 号平面正上方的单个 Orbbec 机位
（RGB 1280×720 @30fps），用于仰泳蹬壁出发的**空中反弓与入水姿态**识别。与三条拼接线
互不依赖，产物写入 `outputs/water_entry/`。

```bash
./scripts/run_water_entry.sh                  # 全流程：预测 → 选帧 → 质检页 → 交付包
./scripts/run_water_entry.sh --skip-predict   # 复用已有预测，只重跑后续
./scripts/run_water_entry.sh --kp 0.10        # 收紧关键点分歧阈值，选出更少
```

数据集默认指向本机路径，可用 `WATER_ENTRY_DATASET_ROOT` 覆盖；它需要
`bk_export_manifest.csv`（唯一的片段清单来源）、`bk_export_202607/` 下的
`<clip>.mp4` 与 `<clip>_res.json`，以及两个 `.pt` 权重。**新增片段后重跑这一个脚本即可
全量刷新**：流程内没有增量状态，不存在只更新一半的可能。

四个流程步骤各自也能单独调：`python -m python.water_entry.{predict,select_frames,annotate_preview,export_package}`，
另有 `review` 出逐帧复核页。

### 三条经过验证的结论

**基准入水帧取 `res.json` 的 `metadata.backstroke.entry_frame`，不是 `manifest.csv` 的
`water_frame`。** 在 `backstroke_applied=False` 的 47 条片段上，`water_frame` 比真入水早
3~36 帧（中位 28 帧）—— 它来自 water_line 掩膜扫描，运动员还在扶壁蜷缩时就会命中。人工
抽帧核对过 `20260717-101123`（manifest 88 / backstroke 119），f88 时人仍在池壁。两个口径
都写进 `metrics.csv` 供对照，窗口取两者并集。

**微调版在空中段与入水段都是满检出，但入水帧判据不是即插即用。** 115 条全量、93 条干净
片段上：`swimup`（现网）flight 0.944 / entry 0.959，`swimup_bk`（微调）1.000 / 0.998，
`coco`（通用）0.739 / 0.482。微调版逐帧检出满分，但入水帧 `|Δ|≤2` 只有 70/93，偏差来自
选人 —— 前排泳道游进的人与岸上教练会抢走轨迹。通用 COCO 模型在**入水段**最差
（entry 仅 0.48），拿它做自动预标注时触水前后那几帧仍需人工补。

**`link_tracks` 的匹配半径固定为画宽的 15%，刻意不随断裂帧数放大。** 放大版实测让
`swimup` 的 12 条片段空中段检出下降、10 条归零（轨迹跨缺口接到画面里的静止目标）。改这个
参数前请先用 `predict` 全量复跑对比 `flight_rate`。

MPS 后端偶发把整窗推理返回全零检测（实测复现 1 次，重跑即恢复）。`predict` 因此分批推理，
且在窗口全空时自动用 CPU 复算一遍再定论，`metrics.csv` 的 `fallback` 列记录是否触发过复算
—— GPU 抖动不应被记成模型失明。
### 难例筛选口径

`select_frames` 只比较 `swimup` 与 `swimup_bk`（待标注数据是给这两个做增量训练的，通用
COCO 的失效模式与我们的训练集无关），逐帧命中七类信号。`score` 取命中信号的最大基础分
（而非求和，避免一堆弱信号压过一个强信号），叠加其余信号一成加成，再乘阶段权重
（入水±3帧 1.6、飞行段 1.25、入水后 1.0、起跳前 0.5）：

| 信号 | 含义 | 基础分 |
| --- | --- | ---: |
| `both_blind` | 两个模型都 0 检出 | 100 |
| `both_reject` | 有检出但选人都没接上 | 70 |
| `one_miss` | 只有一个模型检出 | 60 |
| `diff_person` | 两框 IoU 低于阈值，指向不同的人 | 55 |
| `sign_flip` | 两模型对 sho-hip 符号判断相反 | 50 |
| `kp_disagree` | 同一人但关键点平均分歧超阈值 | 30 |
| `torso_broken` | 有框但躯干四点不全 | 25 |

默认口径在 94 条片段 2613 个窗口内帧中选出 **1163 帧，覆盖全部 94 条**（每片段 min 3 /
中位 10 / p90 20 / max 36），阶段分布 entry 479 / flight 314 / post 249 / pre 121。
**82% 的候选来自 `kp_disagree` 这一个信号** —— 绝大多数帧两模型的框和选人都是对的，差的
只是关节点精度。派给标注员时应强调「在预标注基础上精修关键点」，而不是重画框。

信号在时间上的分工很清楚（`frame - entry_frame` 中位值）：`sign_flip` +2、`kp_disagree` +6，
两者集中在入水帧附近，是最有价值的；`both_blind` +17、`diff_person` +12、`torso_broken` +12
几乎全在入水后，被 `--max-offset 6` 截掉后只剩零星几帧。

三个默认值都是抽帧核对后定的：

- **`--max-offset 6`**：入水 6 帧后运动员已没入水面，两模型开始各自锁住不同的水花伪影。
  实测两框 IoU<0.3 的帧在 offset +6~+12 占 16.6%、+13 之后占 75.3%，而入水前后 ±5 帧一个
  都没有 —— 那种分歧不是姿态质量问题，人工也标不出关键点。
- **`--min-gap 1`（不去重）**：相邻帧画面相似但姿态在变，训练时这种差异有价值。去重只为
  人工翻页方便，需要时用 `--min-gap 3` 压到约三分之一。
- **`--kp-mean-norm 0.055`**：分歧值本身的中位数是 0.0497，阈值压到 0.05 以下等于「一半的
  帧都算难例」，不再是筛选（0.10→323 帧，0.06→936，0.055→1163，0.05→1486）。抽帧核对过
  `0.06~0.08`（439 帧）质量良好，多为一侧模型把肢体关键点画成麻花；`0.05~0.06`（550 帧）
  开始出现两骨架肉眼近乎重合、仅框大小不同的帧，边际价值较低。

默认还排除 `entry_source != "backstroke"` 的 4 条片段（基准入水帧退化成 manifest 的
`water_frame`，偏移量与阶段权重都不可信；其中两条窗口内根本没有出发动作）与 17 条
`suspected_false_positive`。需要纳入前者时加 `--allow-unverified-entry`。

`scripts/run_water_entry.sh` 启动时读取 `select_frames.py` 的 `DEFAULT_*` 常量而不是复制
一份，所以「走流程脚本」与「直接调模块」永远给出同一批候选帧。

`export_package` 导出的是**无叠加原始帧** + `manifest.csv` + COCO keypoints 预标注 + 交付
说明：质检页那套骨架叠加只用于我们自己判断该不该标，真送标注时叠加线条会干扰标注员。预标注
优先取 `swimup_bk`、缺检时退回 `swimup`；置信度低于 `KP_CONF` 的点写成 COCO 的 `v=0`，
标注工具会显示为「待补」而不是一个错误的既有点。

## 数据集标注工具

```bash
./scripts/run_label.sh mask                  # 保留区域 mask 标注器（浏览器）
./scripts/run_label.sh dot                   # 打点标注器（浏览器）
./scripts/run_label.sh merge                 # 快照 → 一张 UV 参考图
./scripts/run_label.sh keypoint              # COCO-17 按人裁剪复核页
```

两个标注器都用 ES module，`file://` 下会被浏览器按 CORS 拦截（origin 为 `null`）导致白屏，
所以必须经这个脚本走 http 打开，不要双击 html。加 `--selftest` 打开该标注器的浏览器自测页。
localhost 同时是 File System Access API 认可的安全上下文，Chrome / Edge 因此能把工程 json
直接写回所选目录。

`merge` 把一台相机全时段的 50 张快照合成一张 UV 参考图：逐像素中值作背景帧，每帧与背景的
RGB 欧氏距离超阈判为前景，按时间顺序叠上去（后帧覆盖前帧）。中值与差分按水平条带算，避免
4K 尺寸下 float32 中间量吃满内存；输出始终是相机原始分辨率。

`keypoint` 从外部标注数据集解析 COCO-17，按人物裁出正方形预览图并叠加骨架、关键点与红色
精准关键点框，产物 `outputs/keypoints/index.html` 双击即可看（图片懒加载，千级裁剪
图不会卡死浏览器）。红框之外的留白来自 `--padding-ratio` 与 `--minimum-side` 算出的裁剪范围。

数据集根用 `SWIM_UNDER_GRIDS_ROOT` 覆盖，产物写入 `outputs/labeling/`。

## 性能取证

```bash
./scripts/run_bench.sh matrix --quick        # 48 格通路验证，每格 1 秒
./scripts/run_bench.sh matrix --duration 15  # 可发布矩阵，每格至少 15 秒
./scripts/run_bench.sh soak                  # 默认十分钟 paced full soak
```

矩阵覆盖六个真实 stage × `1/2/4/6` 路 × paced/unpaced 共 48 格。脚本只构建一次 Release、
只算一次 asset 与各源的 SHA-256，每格写独立 JSONL 并立即校验；任一进程或校验失败即停，
不会静默重试或拼进最终结果。相机清单从 config 的 `source.*` 声明顺序读（与 C++ 加载器
同一口径），路径里的 `${VAR}` 与运行时一样从环境变量展开。

逐路数组（`camera_decoded`、`frame_age_ms_*`、各池的 high-water）每条记录只带**该次运行
实际驱动的路数**，不是数组容量：`render-only` 一路都不驱动，数组就是空的。`source_sha256`
则按**声明的**路数给（部分路数的格子仍指纹全部输入，因为哈希标识的是磁盘上的东西，不是这
一格碰过什么）。结果在 `outputs/benchmarks/runs/<run_id>/`，成功后
`outputs/benchmarks/latest` 指向它：`cells/*.jsonl`（带唯一 `(stage, stream_count, pacing)`
身份的原始格）、`results.jsonl`（48 格全过才生成）、`summary.csv`、`summary.md`、
`manifest.json`（run/build/hash 身份与 `publishable` 标志；不足 15 秒恒为 `false`）。

soak 按每条 interval 的真实 `elapsed_s` 累加时间轴，报告 RSS 与 Metal allocation 的每分钟
线性斜率，并拒绝 host copy、容量 high-water 越界、编码 callback/drain 错误，以及 warm-up 后
连续五个区间低于 29 FPS。默认 RSS 上限 64 MiB/min、Metal allocation 32 MiB/min，可用
`--max-rss-slope` / `--max-gpu-slope` 覆盖。

单独复验已生成的矩阵：
`python -m python.benchmarks.summarize outputs/benchmarks/latest/results.jsonl`。

## 目录结构

```text
swim_fbx_demo/
├── cpp/
│   ├── core/                    # swim_core：可移植 C++20 实时核心
│   ├── backends/{metal,d3d11,cudagl}/   # 按平台隔离，通过 IBackend 契约接入
│   └── app/main.cpp
├── python/
│   ├── common/                  # paths / media / tables / page —— 跨链路共用
│   ├── fbx_tools/               # 唯一 import fbx 的地方（scene、bake_uv）
│   ├── stitch/                  # 三条相机线：profiles 是差异的唯一来源
│   ├── water_entry/             # 入水检测机位
│   ├── labeling/                # 浏览器标注器 + 快照索引
│   ├── keypoints/               # COCO-17 复核页
│   └── benchmarks/              # 指标校验与汇总
├── scripts/
│   ├── run_stitch.{sh,ps1}      # 相机拼接（三条线）
│   ├── run_water_entry.sh       # 入水检测全流程
│   ├── run_label.sh             # 标注工具
│   ├── run_bench.sh             # 性能矩阵 / soak
│   ├── run_win.bat              # Windows 双击入口（走 run_stitch 那条链路）
│   ├── install.bat              # Windows 一键装环境
│   └── checks/check_bat_format.ps1
├── inputs/
│   ├── pool/{models,textures}/  # 项目自带的源模型与合成纹理
│   ├── {underwater,overhead}/models/    # 重资产，本地未入库
│   └── configs/                 # 运行时 config（<line>_<backend>.conf 由脚本生成）
├── tests/{cpp,python,fixtures}/
├── build/                       # 构建树与生成的 .swasset（本机产物）
└── outputs/                     # 全部渲染产物与指标（本机产物）
```

`build/` 与 `outputs/` 都被 gitignore，不放手写源码或文档。

相机数量、相机 ID、输出尺寸都是**数据而非代码**：`kMaxCameras = 16`
（`cpp/core/include/swim/core/camera_capacity.hpp`），config 里 `source.<id>=<path>` 的声明
顺序即通道顺序。新增机位布局应通过 profile + config + `.swasset` 表达，不要在 C++ 里加分支。

## 环境依赖

- Python 3.10 —— 硬要求，不是偏好：Autodesk 只为 cp310 发布 FBX Python SDK 轮子
- Autodesk FBX Python SDK（`import fbx`）、NumPy、OpenCV（`cv2`）、Pillow
- FFmpeg，`ffmpeg` 需在 `PATH`（离线视频拼接用）
- 仅入水检测需要：PyTorch `2.5.1`、TorchVision `0.20.1`、Ultralytics `8.4.x`
  （Apple Silicon 上 MPS 后端可用）
- C++ 侧：CMake 3.25+，macOS 用 Xcode 命令行工具，Windows 用 Visual Studio 2022 +
  Windows 10/11 SDK

### Windows：一键安装

```bat
scripts\install.bat            :: 核心环境
scripts\install.bat pose       :: 追加 torch/ultralytics（约 2.5GB，仅入水检测需要）
scripts\install.bat check      :: 只体检，不改动任何东西
```

七步全部幂等，已就绪的打印 `[SKIP]`，可以反复跑：C++ 工具链体检 → winget 装 Python 3.10 →
把 `.venv` 建/重建到 3.10 → 装 `requirements-win.txt` → 下载并安装 FBX Python SDK →
拉取 `third_party/` 的 FFmpeg+GLFW 并体检 CUDA → 生成网格与 `.swasset`、构建
`swim_realtime.exe`、把运行期 DLL 拷到 exe 旁。

刻意不自动安装 Visual Studio 2022 与 CUDA Toolkit：体积大、要重启、要选组件，只做体检并给出
winget 命令。缺 CUDA 只是警告，此时 `cudagl` 后端被跳过，`d3d11` 不受影响。

### macOS / Linux：手工准备

FBX Python SDK 的发行包取决于操作系统、CPU 架构与 Python ABI，本项目不提供未经验证的通用
安装命令。先确认下面这些通过：

```bash
.venv/bin/python --version
.venv/bin/python -c "import fbx, numpy, cv2, PIL; print('deps OK')"
ffmpeg -version
```

以下都被 gitignore 但运行必需：`outputs/pool/mesh.json`（CMake 的硬依赖，缺了任何 target
都编不过，用 `./scripts/run_stitch.sh pool extract` 生成）、
`inputs/{underwater,overhead}/models/`、`third_party/{ffmpeg,glfw}`、`.venv`、
模型权重 `*.pt`。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests/python -t .   # Python
cmake --build build/metal-release --target swim_core_tests && \
  ./build/metal-release/swim_core_tests                       # C++
cd build/metal-release && ctest                               # CLI 契约与 Metal 冒烟
```

改动实时链路后跑真实数据冒烟，并记录命令与关键指标。判定标准不只看帧率，还要看
`decoded_pixel_host_copies=0`（没把解码像素读回 CPU）、`pool_exhaustion=0`、`malformed=0`。

## 已知限制

- 三条拼接线的相机身份是**位置**对应的，改 FBX 平面相对位置或 profile 的 id 顺序会静默错配。
- 离线视频拼接以最低源 FPS 为输出帧率，对更高帧率输入按最近目标帧位置抽帧；这只对齐帧率，
  不同步采集起始时间（那由 manifest 墙钟负责，见上）。
- H.264 的 `yuv420p` 要求偶数宽高，视频编码阶段可能在画布右侧或底部补一个像素。
- 每组 overhead 样本与一组水下样本的 `align_start_ms` 相差 1~2ms，但两条线各自按自己的
  manifest 对齐，**没有**做跨线路的画面级同步。要严格同帧需要另一层时间轴换算。
- overhead 的样本每组只有 12 秒（水下早期样本有 30 秒的）。`live --seconds` 超过 12 时各路
  会回卷重播；要「播完即止」加 `--no-loop`。
- 入水检测的选人只用位移与轨迹长度，未接入 `res.json` 的 ROI 泳道约束。实测选人错误只有
  2 例，且那两条片段窗口内本就没有出发动作、已被默认排除；但两模型对同一人给出差异极大的
  框在入水 +6 帧之后很常见，那属于水下伪影而非选人缺陷。
- `select_frames` 的信号是模型间分歧，只是错误的**代理**而非错误本身：两模型一致犯错的帧
  不会被选出。
- `suspected_false_positive` 这个标记不能用来解释坏结果：15/17 条的选人几何完全正常
  （位移 +322~+470px），说明那些片段里确实有人跳水，上游为何判为误触发在选人层面看不出来。
- 默认外部数据集路径都是本机绝对路径。换机器后设置对应环境变量：
  `SWIMMING_DATASET_DIR`（pool）、`SWIM_UNDER_GRIDS_ROOT`（水下快照/网格，也可用
  `STITCH_GRID_DIR` 直接指到 grid 目录）、`WATER_ENTRY_DATASET_ROOT`（入水检测）。
- `.venv/` 只保证当前 macOS / Python 3.10 组合；其他平台需自备兼容环境。
