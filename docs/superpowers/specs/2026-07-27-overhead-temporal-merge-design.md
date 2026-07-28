# 高空相机全时段前景合成（overhead temporal merge）设计

日期：2026-07-27

## 目标

把一台相机在整个采集时段的全部快照，合成为**一张**图，供设计师在 Maya 中作为 UV 参考。

现场在池两侧沿长边每隔 0.5 米移动一条白线，每移动一次拍一组快照。因此同一台相机的 50 张图里，白线出现在 50 个不同位置。把这些位置全部叠到一张图上，就得到设计师需要的参考网格。

覆盖三台在**完全相同时刻**拍摄的非水下相机：

| 相机 | 文件名模式 | 分辨率 | 帧数 |
| --- | --- | --- | --- |
| `overhead5` | `1_stitch__under-overhead-xlj__overhead5.jpg` | 3840×2160 | 50 |
| `overhead6` | `2_stitch__under-overhead-xlj__overhead6.jpg` | 3840×2160 | 50 |
| `orbbec_camera_1` | `19_device__orbbec_camera_1__orbbec_camera_1.jpg` | 1280×720 | 50 |

三台相机**各自独立**产出，不做任何几何配准或跨相机拼接。视角差异很大（overhead5 与 overhead6 分别从池两侧俯视），同名像素不对应同一物理点，跨相机融合需要先做池面 homography 标定，不在本设计范围内。

## 与既有水下流程的关系

本任务与 `python/annotation_preview/detect_objects.py` 的水下检测**目标不同**：水下那步是筛选「哪一帧有物体」以供人工打点；这里是把全部帧的前景**叠加**成一张参考图，不做帧级筛选，不判定有无物体。

算法内核（中值背景帧 + 逐像素颜色距离阈值）与 `python/annotation_preview/common.py` 的 `median_dist` 一致，阈值沿用同一个 `DIST_THRESH = 40`。

## 算法

对每台相机独立执行：

1. **枚举帧**：`common.frames_for_camera(cam)` 按 `raw_*` 目录名（含毫秒时间戳）排序返回 `[(snapshot_id, path)]`。该函数通配 `*__<cam>.jpg`，三台相机的文件名都能命中。
2. **中值背景帧**：50 帧逐像素取中值。白线只在 50 个位置各出现一次，中值必然落在无线的池底颜色上，因此背景帧是干净的空池。
3. **前景掩码**：每帧与背景帧的 RGB 欧氏距离 `> 40` 判为前景。
4. **合成**：以背景帧为底，按时间顺序把每帧的前景像素写入，**后帧覆盖前帧**。

前景不做任何形状筛选、连通域分析或人物过滤。现场水中只有两人，其在 50 个时刻的重复出现不影响白线的可读性。这是明确的设计决定，不是简化的权宜之计。

## 内存与分带计算

3840×2160×50 的 uint8 栈是 1.24 GB。但 `load_stack` 先把逐帧解码结果收进 Python list，再用 `np.stack` 一次性拷贝成栈，list 与 stack 会同时存在，实测峰值约为单份栈的 2 倍（约 2.5 GB）；三台相机全分辨率跑完，实测进程峰值 RSS 约 3.46 GiB。中值与差分的 float32 中间量若不分带会到约 5 GB，不可接受。

因此：

- 解码一次进 uint8 栈（`(N, H, W, 3)`）；
- 中值与差分按**水平条带**计算，条带高度可配，默认 256 行；每条带内部才升到 float32；
- 相机**逐个**处理，任一时刻只有一台相机的栈在内存中。

`orbbec_camera_1` 的栈只有 0.14 GB，走完全相同的代码路径，无需特例。

## 不做编号标注

早期版本在合成图上按帧序画过锚点圆点与 `f01` 帧号，并在图下方追加过一条 `f01 → 时间` 的图例带。**该功能已整体移除**：锚点是前景像素坐标的中位数，会被池边人群拉偏，标签落点不可靠；图例带还会把产物撑高到非原始分辨率，交给 Maya 做 UV 时反而添乱。设计师按线的空间顺序从池的一端数过去即可。

产物中不含任何叠加绘制，尺寸严格等于相机原始分辨率。

## 产出

落在 `outputs/annotation_preview/overhead-merge/`（`common.OUTPUT_ROOT` 之下，已被 .gitignore 忽略），每台相机两张 PNG，尺寸均为相机原始分辨率：

| 文件 | 内容 |
| --- | --- |
| `<cam>_background.png` | 中值背景帧 |
| `<cam>_merged.png` | 合成图（**主交付**，无叠加绘制） |

## 代码结构

新模块 `python/annotation_preview/merge_overhead.py`，单一职责：把一台相机的全部快照合成为一张图。

复用 `common.py`：`SNAP_DIR` / `OUTPUT_ROOT` 路径常量、`frames_for_camera`、`DIST_THRESH`。

**不修改 `common.py`**：`median_dist` 的全量加载不适用于 4K 尺寸，分带版本在新模块内实现。两者并存，各自服务于尺寸不同的场景。

模块内的函数边界：

- `median_background(stack, band_rows)` → 背景帧，分带计算，不涉及 IO；
- `merge_frames(stack, background, thresh, band_rows)` → 合成图，分带计算，不涉及 IO；
- `load_stack(paths)` → `(N, H, W, 3) uint8` 栈，保持原始分辨率，不缩放；
- `run_camera(cam, ...)` → 编排 IO 与上述三者，是唯一接触文件系统的函数。

**没有降采样开关**。输出必须是原始分辨率，任何缩放都会让 UV 参考失去意义，所以模块不提供缩放路径。

CLI 参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--cameras` | 三台全跑 | 指定相机 |
| `--thresh` | 40 | 前景判定的 RGB 距离阈值 |
| `--band-rows` | 256 | 分带高度 |
| `--out-dir` | `outputs/annotation_preview/overhead-merge` | 输出目录 |

Shell 入口：`./scripts/run_python.sh oh-merge [...]`，转发全部参数给模块。

## 错误处理

- 快照目录不存在 → 报错退出，提示用 `ANNOTATION_PREVIEW_DATASET_ROOT` 指向有效数据集（与 `detect_objects.py` 一致）。
- 某相机零帧命中 → 打印警告并跳过该相机，不影响其余相机。
- 同一相机各帧尺寸不一致 → 报错退出并列出不一致的帧；不做缩放对齐，尺寸不一致意味着数据有问题，静默处理会产生错误的参考图。
- 三台相机当前均为 50 帧齐全，但代码不假定帧数，按实际命中数处理。

## 测试

`tests/python/test_merge_overhead.py`，用 `tempfile.TemporaryDirectory` 构造若干假快照目录与小尺寸图像（命名遵循真实模式），断言：

1. 中值背景帧的像素值等于各帧对应像素的中值；
2. 超阈像素被**时间上最后**一个超阈帧覆盖；
3. 亚阈位置保留背景帧的值；
4. 分带计算结果与一次性全量计算**逐位相同**（`band_rows` 取 1、素数、大于图高三种情形）；
5. `run_camera` 只生成 `_background` 与 `_merged` 两个文件，输出目录里不存在标注版；
6. 两个产物的尺寸都等于源图分辨率；
7. `--scale` 已被移除，传入该参数时报错退出；
8. 帧尺寸不一致时 CLI 干净退出并带上「帧尺寸不一致」信息，而不是抛裸 traceback。
