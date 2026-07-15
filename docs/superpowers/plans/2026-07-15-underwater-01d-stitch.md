# 水下拼接（underwater stitch）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新建一个与 pool 六路流程任务隔离、但复用其算法的「水下拼接」模块，用 `01d.fbx` 验证 FBX→JSON 提取与 N 块水平拼接静态合成图。

**Architecture:** 新增 `python/underwater/` 包，通过 import 复用 `python.assets.extract_fbx`（提取）和 `python.validation.reference_renderer`（remap/羽化/合成/网格）的算法函数，不修改这两个既有模块。提取入口按世界 X 从左到右排序网格以支持后续 16 块扩展；渲染入口默认自适应到约 640 宽输出，产物全部写入独立目录 `outputs/underwater/`。

**Tech Stack:** Python 3.10、Autodesk FBX Python SDK（仅提取用）、NumPy、OpenCV（`cv2`）、unittest。

## Global Constraints

- 不修改 `python/assets/extract_fbx.py`、`python/validation/reference_renderer.py`、`scripts/run_python.sh`、`compile_runtime_asset.py`；复用只能 import，不能复制算法实现。
- 复用的输出 JSON 结构必须与 pool 一致：`{"source": str, "meshes": [{"node", "texture_basename", "uvset", "const_axis", "kept_axes", "spans", "triangles"}, ...]}`；`triangles` 为 `[[{"pos":[x,y],"uv":[u,v]}, x3], ...]`。
- 默认输入：源模型 `inputs/models/01d.fbx`，纹理目录 `inputs/models/01d.fbm`（含 `underA1-grid.png`、`underA2-grid.png`，均 640×360）。
- 所有产物写入 `outputs/underwater/`，不得写入 `outputs/data`、`outputs/images`。
- 测试用 `.venv/bin/python -m unittest`；依赖 FBX SDK 的测试在 `import fbx` 失败时必须 `skipUnless` 跳过，不得让整套测试崩溃。
- 网格顺序不依赖 FBX 节点声明顺序，一律按每块三角形顶点 `pos[0]`（世界 X 投影）的最小值升序排列。

---

### Task 1: 包骨架与网格排序函数

**Files:**
- Create: `python/underwater/__init__.py`
- Create: `python/underwater/extract.py`
- Test: `python/tests/test_underwater.py`

**Interfaces:**
- Consumes: 无（首个任务）。
- Produces: `python.underwater.extract.sort_meshes_by_world_x(meshes: list[dict]) -> list[dict]` —— 输入 pool 结构的 meshes 列表，返回按每块三角形顶点 `pos[0]` 最小值升序排序的新列表（不修改入参）。空块（无三角形）排在最后。

- [ ] **Step 1: 建包 `__init__.py`**

创建空文件 `python/underwater/__init__.py`（内容：单行注释）。

```python
"""Underwater stitch task: FBX extraction and N-plane horizontal composite, reusing pool algorithms."""
```

- [ ] **Step 2: 写失败测试**

创建 `python/tests/test_underwater.py`：

```python
import unittest

from python.underwater.extract import sort_meshes_by_world_x


def _mesh(node, x0):
    # single triangle whose min pos[0] is x0
    tri = [
        {"pos": [x0, 0.0], "uv": [0.0, 0.0]},
        {"pos": [x0 + 1.0, 0.0], "uv": [1.0, 0.0]},
        {"pos": [x0, 1.0], "uv": [0.0, 1.0]},
    ]
    return {"node": node, "texture_basename": f"{node}.png", "triangles": [tri]}


class SortMeshesTest(unittest.TestCase):
    def test_orders_left_to_right_by_world_x(self):
        meshes = [_mesh("right", 5.0), _mesh("left", -2.0), _mesh("mid", 1.0)]
        ordered = sort_meshes_by_world_x(meshes)
        self.assertEqual([m["node"] for m in ordered], ["left", "mid", "right"])

    def test_does_not_mutate_input(self):
        meshes = [_mesh("right", 5.0), _mesh("left", -2.0)]
        sort_meshes_by_world_x(meshes)
        self.assertEqual([m["node"] for m in meshes], ["right", "left"])

    def test_empty_triangles_sort_last(self):
        empty = {"node": "empty", "texture_basename": "e.png", "triangles": []}
        meshes = [empty, _mesh("left", -2.0)]
        ordered = sort_meshes_by_world_x(meshes)
        self.assertEqual([m["node"] for m in ordered], ["left", "empty"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo && .venv/bin/python -m unittest python.tests.test_underwater -v`
Expected: FAIL —— `ModuleNotFoundError` 或 `ImportError: cannot import name 'sort_meshes_by_world_x'`。

- [ ] **Step 4: 实现 `extract.py` 的排序函数**

创建 `python/underwater/extract.py`，先只放排序函数与依赖导入：

```python
"""Extract 01d-style FBX into pool-compatible mesh JSON, ordered left-to-right.

Reuses python.assets.extract_fbx for all FBX/UV/geometry logic; this module only
adds underwater-specific defaults, left-to-right ordering, and an isolated CLI.
"""
import argparse
import json
import sys
from pathlib import Path

from python.assets import extract_fbx, fbx_common

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"


def _mesh_min_x(mesh):
    xs = [v["pos"][0] for tri in mesh["triangles"] for v in tri]
    return min(xs) if xs else float("inf")


def sort_meshes_by_world_x(meshes):
    """Return meshes ordered by each mesh's minimum world-X (pos[0]) ascending.

    Empty meshes (no triangles) sort last. Input list is not mutated."""
    return sorted(meshes, key=_mesh_min_x)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo && .venv/bin/python -m unittest python.tests.test_underwater -v`
Expected: PASS（3 tests）。

- [ ] **Step 6: 提交**

```bash
cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo
git add python/underwater/__init__.py python/underwater/extract.py python/tests/test_underwater.py
git commit -m "feat(underwater): add package skeleton and left-to-right mesh ordering

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 提取 CLI（`extract.py` main）

**Files:**
- Modify: `python/underwater/extract.py`
- Test: `python/tests/test_underwater.py`

**Interfaces:**
- Consumes: `sort_meshes_by_world_x` (Task 1); `extract_fbx.walk(node, out_list, tex_dir)`（就地 append 每个 mesh 的 `extract_mesh` 结果到 `out_list`）; `extract_fbx.display_path(path) -> str`; `fbx_common.InitializeSdkObjects() -> (mgr, scene)`; `fbx_common.LoadScene(mgr, scene, str_path) -> bool`。
- Produces: `python.underwater.extract.extract_to_json(src: Path, dst: Path, tex_dir: Path) -> list[dict]` —— 加载 FBX、提取、按世界 X 排序、写 JSON（pool 结构），返回排序后的 meshes；`main(argv=None)` CLI 包装。

- [ ] **Step 1: 写 FBX 集成失败测试（追加到 test_underwater.py）**

在 `python/tests/test_underwater.py` 顶部导入区补充：

```python
from pathlib import Path

try:
    import fbx  # noqa: F401
    HAS_FBX = True
except Exception:
    HAS_FBX = False

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL_01D = PROJECT_ROOT / "inputs" / "models" / "01d.fbx"
TEXDIR_01D = PROJECT_ROOT / "inputs" / "models" / "01d.fbm"
```

追加测试类：

```python
class ExtractIntegrationTest(unittest.TestCase):
    @unittest.skipUnless(HAS_FBX, "FBX SDK not available")
    @unittest.skipUnless(MODEL_01D.is_file(), "01d.fbx not present")
    def test_extracts_two_ordered_meshes(self):
        import tempfile
        from python.underwater.extract import extract_to_json

        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "01d_mesh.json"
            meshes = extract_to_json(MODEL_01D, dst, TEXDIR_01D)

            self.assertTrue(dst.is_file())
            self.assertEqual(len(meshes), 2)
            # ordered left-to-right by world X: Box001 (min x ~ -0.57) before pPlane1 (~ -0.43)
            self.assertEqual(
                [m["node"] for m in meshes], ["Box001", "pPlane1"]
            )
            self.assertEqual(
                [m["texture_basename"] for m in meshes],
                ["underA2-grid.png", "underA1-grid.png"],
            )
            self.assertEqual([m["uvset"] for m in meshes], ["UVChannel_1", "map1"])
```

（顺序依据：实测 Box001 世界 X ∈ [-0.575, 0.279]、pPlane1 ∈ [-0.427, 0.427]，故 Box001 的 min-x 更小，排在前。）

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo && .venv/bin/python -m unittest python.tests.test_underwater.ExtractIntegrationTest -v`
Expected: FAIL —— `ImportError: cannot import name 'extract_to_json'`（若 SDK/模型不存在则 SKIP，此时手动确认 SKIP 原因后继续实现）。

- [ ] **Step 3: 实现 `extract_to_json` 与 `main`（追加到 extract.py 末尾）**

```python
def extract_to_json(src, dst, tex_dir):
    src = Path(src)
    dst = Path(dst)
    tex_dir = Path(tex_dir)
    if not src.is_file():
        raise SystemExit(f"source file does not exist: {src}")
    if not tex_dir.is_dir():
        raise SystemExit(f"texture directory does not exist: {tex_dir}")

    mgr, scene = fbx_common.InitializeSdkObjects()
    if not fbx_common.LoadScene(mgr, scene, str(src)):
        raise SystemExit(f"FAILED to load {src}")
    meshes = []
    extract_fbx.walk(scene.GetRootNode(), meshes, tex_dir)
    mgr.Destroy()

    if not meshes:
        raise SystemExit(f"no mesh found in {src}")
    meshes = sort_meshes_by_world_x(meshes)

    data = {"source": extract_fbx.display_path(src), "meshes": meshes}
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(str(dst), "w", encoding="utf-8") as f:
        json.dump(data, f)
    for m in meshes:
        print(f"{m['node']:12s} tris={len(m['triangles']):4d} "
              f"tex={m['texture_basename']} uvset={m['uvset']}")
    print("wrote", dst)
    return meshes


def main(argv=None):
    ap = argparse.ArgumentParser(description="Extract underwater FBX to mesh JSON")
    ap.add_argument("src", nargs="?", type=Path, default=INPUTS_DIR / "models" / "01d.fbx")
    ap.add_argument("dst", nargs="?", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_mesh.json")
    ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "models" / "01d.fbm")
    args = ap.parse_args(argv)
    extract_to_json(args.src, args.dst, args.tex_dir)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo && .venv/bin/python -m unittest python.tests.test_underwater -v`
Expected: PASS 或（无 SDK 时）ExtractIntegrationTest SKIP，其余 PASS。

- [ ] **Step 5: 真机跑一次提取，确认 JSON 落盘**

Run: `cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo && .venv/bin/python -m python.underwater.extract`
Expected: 打印两行 mesh（Box001 先、pPlane1 后），并 `wrote .../outputs/underwater/01d_mesh.json`。

- [ ] **Step 6: 提交**

```bash
cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo
git add python/underwater/extract.py python/tests/test_underwater.py
git commit -m "feat(underwater): add FBX extraction CLI reusing extract_fbx

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 渲染 CLI（`render.py`）— 静图 + 网格图 + 自适应 ppm

**Files:**
- Create: `python/underwater/render.py`
- Test: `python/tests/test_underwater.py`

**Interfaces:**
- Consumes: `reference_renderer.to_meters(meshes, unit_scale, neg_v)`（就地把 pos 除以 unit_scale、按 neg_v 翻转 Y）; `reference_renderer.world_bounds(meshes) -> (xmin, xmax, ymin, ymax)`; `reference_renderer.build_remap(mesh, tex_w, tex_h, xmin, ymin, ppm, out_w, out_h) -> (m1, m2, mask)`; `reference_renderer.feather_weights(masks) -> list`; `reference_renderer.composite(layers, weights, frames, out_h, out_w) -> ndarray`; `reference_renderer.draw_grid(img, meshes, xmin, ymin, ppm, out_h) -> img`; `reference_renderer.write_image(path, image, kind)`.
- Produces: `python.underwater.render.resolve_ppm(xmin, xmax, target_width) -> float`（世界 X 跨度→约 target_width 宽的 ppm，跨度≤0 时回退 100.0）; `render_stills(data_path, tex_dir, still_path, grid_path, ppm=None, unit_scale=1.0, neg_v=True, target_width=640) -> (out_w, out_h)`; `main(argv=None)` CLI。

- [ ] **Step 1: 写 render 算法测试（追加到 test_underwater.py）**

追加导入与测试类（不依赖 FBX SDK；用合成 mesh JSON + 纯色小图）：

```python
class RenderStillTest(unittest.TestCase):
    def test_resolve_ppm_targets_width(self):
        from python.underwater.render import resolve_ppm
        # world X span 2.0 -> ppm ~ 320 for 640 target
        self.assertAlmostEqual(resolve_ppm(-1.0, 1.0, 640), 320.0, places=3)

    def test_resolve_ppm_degenerate_span_falls_back(self):
        from python.underwater.render import resolve_ppm
        self.assertEqual(resolve_ppm(0.0, 0.0, 640), 100.0)

    def test_render_writes_still_and_grid(self):
        import json
        import tempfile
        import cv2
        import numpy as np
        from python.underwater.render import render_stills

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # one unit-square mesh mapped to a full texture
            tri_a = [
                {"pos": [0.0, 0.0], "uv": [0.0, 0.0]},
                {"pos": [1.0, 0.0], "uv": [1.0, 0.0]},
                {"pos": [1.0, 1.0], "uv": [1.0, 1.0]},
            ]
            tri_b = [
                {"pos": [0.0, 0.0], "uv": [0.0, 0.0]},
                {"pos": [1.0, 1.0], "uv": [1.0, 1.0]},
                {"pos": [0.0, 1.0], "uv": [0.0, 1.0]},
            ]
            data = {"source": "x", "meshes": [
                {"node": "p", "texture_basename": "t.png", "uvset": "map1",
                 "const_axis": 2, "kept_axes": [0, 1], "spans": [1, 1, 0],
                 "triangles": [tri_a, tri_b]},
            ]}
            data_path = td / "mesh.json"
            data_path.write_text(json.dumps(data))
            tex = np.full((16, 16, 3), 200, np.uint8)
            cv2.imwrite(str(td / "t.png"), tex)

            still = td / "out_stitch.png"
            grid = td / "out_grid.png"
            out_w, out_h = render_stills(
                data_path, td, still, grid, ppm=None,
                unit_scale=1.0, neg_v=False, target_width=64,
            )
            self.assertTrue(still.is_file())
            self.assertTrue(grid.is_file())
            img = cv2.imread(str(still))
            self.assertEqual(img.shape[1], out_w)
            self.assertEqual(img.shape[0], out_h)
            self.assertGreater(int(img.max()), 0)  # not all black
```

（注意：测试用 `neg_v=False` 避免翻转把单位方块推到画布外；`const_axis/kept_axes/spans` 字段虽 render 不直接用，但保持 pool 结构完整。）

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo && .venv/bin/python -m unittest python.tests.test_underwater.RenderStillTest -v`
Expected: FAIL —— `ModuleNotFoundError: python.underwater.render`。

- [ ] **Step 3: 实现 `render.py`**

创建 `python/underwater/render.py`：

```python
"""Render the underwater stitch from a mesh JSON: still + grid diagnostic.

Reuses python.validation.reference_renderer for all remap/feather/composite/grid
logic; this module only adds underwater defaults, width-adaptive ppm, and an
isolated CLI that writes into outputs/underwater/.
"""
import argparse
import json
from pathlib import Path

import cv2

from python.validation import reference_renderer as rr

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"


def resolve_ppm(xmin, xmax, target_width):
    """Pixels-per-metre so world-X span maps to ~target_width px. Fallback 100.0."""
    span = xmax - xmin
    if span <= 0:
        return 100.0
    return target_width / span


def render_stills(data_path, tex_dir, still_path, grid_path,
                  ppm=None, unit_scale=1.0, neg_v=True, target_width=640):
    data_path = Path(data_path)
    tex_dir = Path(tex_dir)
    if not data_path.is_file():
        raise SystemExit(f"data file does not exist: {data_path}")
    with open(str(data_path), encoding="utf-8") as f:
        meshes = json.load(f)["meshes"]

    rr.to_meters(meshes, unit_scale, neg_v)
    xmin, xmax, ymin, ymax = rr.world_bounds(meshes)
    if ppm is None:
        ppm = resolve_ppm(xmin, xmax, target_width)
    out_w = int(round((xmax - xmin) * ppm)) + 1
    out_h = int(round((ymax - ymin) * ppm)) + 1
    print(f"canvas {out_w}x{out_h} @ {ppm:.2f}px/m")

    texs = []
    for m in meshes:
        path = tex_dir / m["texture_basename"]
        texture = cv2.imread(str(path))
        if texture is None:
            raise SystemExit(f"cannot read texture: {path}")
        texs.append(texture)
    layers = [rr.build_remap(m, t.shape[1], t.shape[0], xmin, ymin, ppm, out_w, out_h)
              for m, t in zip(meshes, texs)]
    wts = [w[..., None] for w in rr.feather_weights([l[2] for l in layers])]
    comp = rr.composite(layers, wts, [t.astype("float32") for t in texs], out_h, out_w)

    if still_path is not None:
        still_path = Path(still_path)
        still_path.parent.mkdir(parents=True, exist_ok=True)
        rr.write_image(still_path, comp, "still")
        print(f"wrote still {still_path}")
    if grid_path is not None:
        grid_path = Path(grid_path)
        grid = rr.draw_grid(comp.copy(), meshes, xmin, ymin, ppm, out_h)
        grid_path.parent.mkdir(parents=True, exist_ok=True)
        rr.write_image(grid_path, grid, "grid")
        print(f"wrote grid still {grid_path}")
    return out_w, out_h


def main(argv=None):
    ap = argparse.ArgumentParser(description="Render underwater stitch still + grid")
    ap.add_argument("--data", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_mesh.json")
    ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "models" / "01d.fbm")
    ap.add_argument("--still", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_stitch.png")
    ap.add_argument("--grid-still", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "01d_grid.png")
    ap.add_argument("--ppm", type=float, default=None,
                    help="pixels per metre; default adapts world-X span to --target-width")
    ap.add_argument("--target-width", type=int, default=640)
    ap.add_argument("--unit-scale", type=float, default=1.0)
    ap.add_argument("--no-neg-v", dest="neg_v", action="store_false", default=True)
    args = ap.parse_args(argv)
    render_stills(args.data, args.tex_dir, args.still, args.grid_still,
                  ppm=args.ppm, unit_scale=args.unit_scale, neg_v=args.neg_v,
                  target_width=args.target_width)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo && .venv/bin/python -m unittest python.tests.test_underwater -v`
Expected: PASS（RenderStillTest 3 个全过；SortMeshes 3 个；Extract 依 SDK 存在与否 PASS/SKIP）。

- [ ] **Step 5: 提交**

```bash
cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo
git add python/underwater/render.py python/tests/test_underwater.py
git commit -m "feat(underwater): add still+grid renderer reusing reference_renderer

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 端到端产出与验证（01d 真机合成图）

**Files:**
- 无新增源码（仅运行 + 目测 + 全量回归）。

**Interfaces:**
- Consumes: Task 2 的 `extract` CLI、Task 3 的 `render` CLI。
- Produces: `outputs/underwater/01d_mesh.json`、`01d_stitch.png`、`01d_grid.png`（未纳入 git 跟踪，产物目录）。

- [ ] **Step 1: 运行提取（若 Task 2 已生成可跳过重跑）**

Run: `cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo && .venv/bin/python -m python.underwater.extract`
Expected: `wrote .../outputs/underwater/01d_mesh.json`，两块 mesh。

- [ ] **Step 2: 运行渲染**

Run: `cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo && .venv/bin/python -m python.underwater.render`
Expected: 打印 `canvas <约640>x<H> @ <ppm>px/m`，`wrote still ...01d_stitch.png`、`wrote grid still ...01d_grid.png`。

- [ ] **Step 3: 目测产物**

用 Read 工具查看 `outputs/underwater/01d_stitch.png` 与 `01d_grid.png`：确认两块平面按左右铺开、重叠区羽化过渡自然、UV 无明显翻转/错位；网格图的三角形与区域轮廓与静图内容对齐。若发现整体上下颠倒，记录并用 `--no-neg-v` 复跑对比，选视觉正确者作为默认（如需改默认则回到 Task 3 调整并补测试）。

- [ ] **Step 4: 全量回归**

Run: `cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo && .venv/bin/python -m unittest discover -s python/tests -v`
Expected: 全绿（新 underwater 测试 + 既有测试；无 SDK 环境下 Extract 集成测试 SKIP）。

- [ ] **Step 5: 更新 README 增加水下拼接段落**

在 `README.md` 末尾追加一节 `## 水下拼接（underwater stitch）`，说明：任务与 pool 隔离、复用算法；命令
`./`（直接 `python -m python.underwater.extract` 与 `python -m python.underwater.render`）；默认输入 `inputs/models/01d.fbx` + `inputs/models/01d.fbm`；产物在 `outputs/underwater/`；网格按世界 X 左→右排序，后续可扩展到 16 块。

- [ ] **Step 6: 提交**

```bash
cd /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo
git add README.md
git commit -m "docs: document underwater stitch pipeline

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- 独立模块 → Task 1（包）+ Task 2/3（入口）。
- FBX→JSON 提取复用 → Task 2（import `extract_fbx.walk`）。
- 静图 + 网格诊断图 → Task 3。
- 世界 X 左→右排序（16 块预留）→ Task 1 + Task 2。
- 自适应 640 宽 → Task 3 `resolve_ppm`。
- 重叠区羽化混合 → Task 3 复用 `feather_weights`/`composite`。
- 产物隔离 `outputs/underwater/` → Task 2/3 默认路径。
- 单元测试（render 不依赖 SDK + 排序 + 提取集成 skipUnless）→ Task 1/2/3。
- 错误处理（缺文件/空网格/缺纹理/缺 JSON）→ Task 2 `extract_to_json`、Task 3 `render_stills`。
- 不碰 pool/6 路硬编码 → 全程仅 import，未修改既有模块。

**Placeholder scan:** 无 TBD/TODO；所有代码步骤含完整代码；命令与预期均具体。

**Type consistency:** `sort_meshes_by_world_x`(Task1→Task2)、`extract_to_json`/`main`(Task2)、`resolve_ppm`/`render_stills`/`main`(Task3) 名称与签名跨任务一致；复用的 `reference_renderer` 与 `extract_fbx` 函数签名与源码核对无误。

**已知取舍：** `GRID_COLORS` 仅 6 色，16 块时 `draw_grid` 用 `idx % len` 循环复用颜色，属诊断退化，本轮不扩（YAGNI）。
