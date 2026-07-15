---
title: 水下拼接（underwater stitch）— 01d 验证与 N 块水平拼接设计
date: 2026-07-15
status: draft
---

# 水下拼接（underwater stitch）设计文档

## 背景

现有仓库是「六路 4K 泳池拼接」项目：`python/assets/extract_fbx.py` 把 `pool.fbx`
的 6 块平面网格与 UV 提取为 JSON，`python/validation/reference_renderer.py` 用
NumPy/OpenCV 的 remap + 羽化把 6 张合成纹理或 6 路视频拼成俯视图。

现在启动一个**全新任务「水下拼接」**，用 `inputs/models/01d.fbx` 做验证。它与
pool 在**任务上完全隔离**，但在**算法代码上尽量复用、去冗余**（import 复用，不复制）。
01d 当前是左右两块平面，**后续会扩展到 16 块平面水平依次连接**，因此设计必须从一开始
就按「N 块水平拼接」来做，而不是写死 2 块或 6 块。

## 新旧 FBX 差异（已实测）

用 FBX SDK 读取两模型对比：

| 维度 | pool.fbx（旧） | 01d.fbx（新，验证用） |
| --- | --- | --- |
| FBX 格式 | ASCII | 二进制 Kaydara 7400 |
| 网格数 | 6（`01/02/03/u/Plane004/Plane007`） | 2（`pPlane1`/`Box001`），将扩到 16 |
| 纹理 | 6 张 `camera_N_composite.png` | 2 张 `underA1-grid.png`/`underA2-grid.png`，均 640×360 |
| 纹理引用 | basename 命中 `inputs/textures/` | FBX 内嵌绝对路径 `Y:\Baidu\...`，SDK 已回退到同级 `01d.fbm/` |
| UV set 名 | 全部 `UVChannel_1` | 混用 `map1`（pPlane1）与 `UVChannel_1`（Box001） |
| 世界朝向 | Y-up | Z-up；`detect_constant_axis` 实测选出常量轴=Y、保留轴=[X,Z]，投影仍正确 |
| UV 范围 | 略超 [0,1]（中线延伸烘焙） | 近似 [0,1]，A2 的 V 有 -0.0115 微溢出 |

**关键结论**：现有 `extract_fbx.py` 的提取逻辑（`detect_constant_axis` /
`texture_uvset_name` / `pick_uv_element` / `uv_at` / `extract_mesh`）已能无改动读出
01d 的两块网格、UV 与纹理绑定（已在内存中跑通，未写文件）。差异只在**网格数量**、
**纹理目录**（`01d.fbm`）、以及**两块平面在世界 X 上有重叠**。硬编码「6 路」的只有
`run_python.sh` 与 `compile_runtime_asset.py`，本任务不触碰它们。

## 目标与范围

**本轮做**：
1. FBX → JSON 提取（复用 pool 算法，独立入口）。
2. 静态合成图（世界坐标下 N 块按 X 从左到右排布，重叠区羽化混合）。
3. 网格诊断图（叠加三角形边 + 每块区域轮廓）。
4. 单元测试。

**本轮不做**：视频链路、Metal 运行时资产、16 块真实数据接入（等新数据就绪）。当前
以 01d 的 2 块作为「N=2 的实例」验证 N 块通路。

## 架构

新建 `python/underwater/`，与 `python/assets`、`python/validation` 平级，
**不修改**这两个既有目录。复用通过 import 完成：

```
python/underwater/
├── __init__.py
├── extract.py     # CLI: 01d.fbx -> outputs/underwater/01d_mesh.json
└── render.py      # CLI: JSON -> 静图 + 网格图
```

### extract.py

- 复用 `python.assets.extract_fbx` 的 `walk` / `extract_mesh`（连同其依赖的
  `detect_constant_axis` / `texture_uvset_name` / `pick_uv_element` / `uv_at`）。
- 差异化仅在 `main`：
  - 默认 `src = inputs/models/01d.fbx`，默认 `--tex-dir = inputs/models/01d.fbm`。
  - 默认 `dst = outputs/underwater/01d_mesh.json`。
  - **提取后按世界 X 最小值升序排序 meshes**（为 16 块「水平依次连接」提供稳定的
    左→右顺序，不依赖 FBX 节点声明顺序）。排序键用每块三角形顶点 `pos[0]` 的最小值。
- 输出 JSON 结构与 pool 完全一致（`{"source":..., "meshes":[{node, texture_basename,
  uvset, const_axis, kept_axes, spans, triangles}, ...]}`），以便 render 复用。

### render.py

- 复用 `reference_renderer` 的 `to_meters` / `world_bounds` / `build_remap` /
  `feather_weights` / `composite` / `draw_grid` / `write_image`。
- CLI 参数：`--data`（默认 `outputs/underwater/01d_mesh.json`）、`--tex-dir`（默认
  `inputs/models/01d.fbm`）、`--still`（默认 `outputs/underwater/01d_stitch.png`）、
  `--grid-still`（默认 `outputs/underwater/01d_grid.png`）、`--ppm`、`--unit-scale`、
  `--no-neg-v`。
- **默认 ppm 自适应到约 640 宽输出**：读入后按世界 X 跨度计算
  `ppm = round(640 / (xmax-xmin))`，避免对 640×360 源纹理做无意义放大；用户可显式覆盖。
- 合成语义：保留世界坐标，N 块按各自世界位置铺在同一画布上，**重叠区由
  `feather_weights` 羽化混合**，无重叠处保持硬边（与 pool 中线接缝同机制）。

### 产物隔离

全部写入 **新目录 `outputs/underwater/`**，不写 `outputs/data` / `outputs/images`，
与 pool 产物零交叉。

## 数据流

```
01d.fbx + 01d.fbm/*.png
   │  extract.py（复用 extract_fbx 提取 + 按世界X排序）
   ▼
outputs/underwater/01d_mesh.json
   │  render.py（复用 reference_renderer 的 remap/feather/composite）
   ▼
outputs/underwater/01d_stitch.png   （静态合成图）
outputs/underwater/01d_grid.png     （三角网格 + 区域轮廓诊断图）
```

## 错误处理

沿用 renderer 现有风格，明确报错退出（`SystemExit`）：
- 源 FBX 不存在 / 纹理目录不存在。
- 提取到的网格数为 0。
- render 时某块 `texture_basename` 在 `--tex-dir` 下找不到或读不出。
- `--data` JSON 不存在。

## 测试

新增 `python/tests/test_underwater.py`：
1. **render 算法测试（不依赖 FBX SDK）**：构造极小合成 mesh JSON（1~2 个三角形）
   + 纯色小图，跑 render 主流程，断言静图与网格图文件生成、尺寸为预期
   `out_w×out_h`、内容非全零。
2. **N 块顺序测试（不依赖 FBX SDK）**：构造两块世界 X 位置相反的 mesh，调用 extract
   的排序函数，断言输出按世界 X 升序（左→右），保证 16 块场景顺序稳定。
3. **提取器集成测试（依赖 FBX SDK，缺失时 `skipUnless` 跳过）**：对 01d.fbx 提取，
   断言恰好 2 块、纹理 basename 分别为 `underA1-grid.png`/`underA2-grid.png`、
   uvset 分别为 `map1`/`UVChannel_1`。

回归：`.venv/bin/python -m unittest discover -s python/tests -v` 全绿。

## 调试路径

1. 先看 640 宽静图：肉眼确认两块相对位置、接缝/重叠是否自然、UV 有无翻转错位。
2. 再看网格图：三角形边与区域轮廓是否与静图内容对齐。
3. 若朝向或翻转异常，用 `--no-neg-v` / `--unit-scale` 排查坐标系差异（Z-up）。

## 对 16 块扩展的预留

- 网格顺序由世界 X 排序决定，天然支持任意 N。
- render 的 `build_remap`/`feather_weights`/`composite` 与块数无关。
- `GRID_COLORS` 只有 6 色，draw_grid 已用 `idx % len` 循环取色；16 块时颜色会复用，
  属可接受的诊断退化，本轮不扩色板（YAGNI，待真实 16 块数据再定）。
