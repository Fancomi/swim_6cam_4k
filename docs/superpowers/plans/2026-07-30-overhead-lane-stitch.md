# 俯视水道拼接（overhead lane stitch）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `inputs/002.fbx` 的两块俯视平面接成一条水道全景，离线与实时两条通路都跑通；同时把 `python/underwater/` 提炼为 profile 驱动的 `python/stitch/`，使第三条拼接线路只需增加一条声明。

**Architecture:** `002.fbx` 与水下 16 平面是同一类几何问题（N 块平面横向一字排开，同一条 25.000m × 3.000m 水道），现有几何/权重/裁剪代码原样即可跑通它。因此本计划不写新算法，只做三件事：把两条线路的差异集中到 `profiles.py` 的数据记录里；删掉三处写死水下的代码（贴图名正则、模块常量、无条件读 manifest）；用一张步骤表取代手写的 `uw-*` 子命令笛卡尔积。

**Tech Stack:** Python 3.10（`.venv`）、NumPy、OpenCV（`cv2`）、Autodesk FBX Python SDK、FFmpeg CLI、CMake + Ninja、Metal（C++ 侧零改动）、`unittest`。

## Global Constraints

- 设计文档：`docs/superpowers/specs/2026-07-30-overhead-lane-stitch-design.md`，本计划的每个数值都以它为准。
- 解释器一律 `.venv/bin/python`；测试命令一律 `.venv/bin/python -m unittest`（本仓库**没有** pytest）。
- 工作目录一律仓库根 `/Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo`。
- 基线：`.venv/bin/python -m unittest discover -s tests/python -t .` 当前 **180 passed**，其中 `test_underwater.py` **33 个**。任何任务结束时这个数字只能增不能减。
- 不改任何 C++ 代码、不改 `CMakeLists.txt` 的 `pool_4k.swasset` 规则、不改 `python/validation/`、不改 `python/water_entry/`、不改 `python/annotation_preview/`。
- pool 六路**不进** profile 注册表（两排布局、相机序非 world-X 升序、距离变换羽化）。
- overhead profile 的既定数值：`ppm=170.0`、`blend_px=85.0`、`full_res=False`、`crop_bottom="none"`、`clip_uv=True`、`planes_only=False`、`sync="none"`、`source_size=(3840, 2160)`、`camera_ids=("cam5", "cam6")`、`clip_suffix=".mp4"`。
- underwater profile 的数值必须与现状逐一相等：`ppm=240.0`、`blend_px=120.0`、`full_res=True`、`crop_bottom="auto"`、`clip_uv=True`、`planes_only=True`、`sync="manifest"`、`source_size=(1280, 720)`、`camera_ids=underA16…underA1`、`clip_suffix=".ts"`。
- 4K 数据集：`/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K`，会话前缀 `20260629_172532`，本计划只用其中 `cam5`/`cam6` 两路。
- 测试里构造「两块平面 mesh JSON」的 helper 只写一份，放在 `tests/python/test_stitch.py`
  的**模块层**（与文件里已有的 `_mesh` / `_band_plane` / `_strip` 同级），三个测试类共用：

  ```python
  def _plane(node, tex, x0, width=1.0, y0=0.0, y1=1.0):
      """One quad, two triangles, UV spanning the full texture."""
      corners = [
          [{"pos": [x0, y0], "uv": [0.0, 0.0]},
           {"pos": [x0 + width, y0], "uv": [1.0, 0.0]},
           {"pos": [x0 + width, y1], "uv": [1.0, 1.0]}],
          [{"pos": [x0, y0], "uv": [0.0, 0.0]},
           {"pos": [x0 + width, y1], "uv": [1.0, 1.0]},
           {"pos": [x0, y1], "uv": [0.0, 1.0]}],
      ]
      return {"node": node, "texture_basename": tex, "uvset": "UVChannel_1",
              "const_axis": 2, "kept_axes": [0, 1], "spans": [width, y1 - y0, 0],
              "triangles": corners}


  def _two_plane_json(td, planes):
      """Write a mesh JSON of `planes` (already built by _plane) and return it."""
      import json

      path = Path(td) / "mesh.json"
      path.write_text(json.dumps({"source": "test", "meshes": list(planes)}))
      return path
  ```

  Task 3 建立这两个函数；Task 4 与 Task 8 直接调用，不再各写一份。
- 提交信息用英文，正文说清「为什么」；不写 `Generated with` 之类的尾注。

---

## 文件结构

改动集中在两处：`python/stitch/`（原 `python/underwater/`）与 `scripts/`。

| 文件 | 责任 |
| --- | --- |
| `python/stitch/profiles.py` | **新增。** `Profile` 数据类 + 两条注册记录 + `get(name)`。一条拼接线路的全部差异只在这里声明。 |
| `python/stitch/__main__.py` | **新增。** 步骤 dispatcher：`python -m python.stitch <profile> <steps>`。把 profile 的默认值填进各步骤的实参。 |
| `python/stitch/extract.py` | 原样搬迁，仅改模块文档串里的示例路径。 |
| `python/stitch/render.py` | 原样搬迁，零改动（几何/权重/裁剪全部与线路无关）。 |
| `python/stitch/render_video.py` | 删 `camera_of()`；相机序改为按位置取自入参；`align` 改由调用方按 `profile.sync` 决定。 |
| `python/stitch/export_ref_tex.py` | 原 `export_real_tex.py`。泛化出两种来源：数据集快照（水下）与视频首帧（overhead）。 |
| `python/stitch/run.py` | 模块常量 → profile 字段；`write_config` 的 glob 后缀、lane 顺序、asset 路径全部取自 profile。 |
| `python/assets/compile_runtime_asset.py:13` | 仅改 import 路径（`python.underwater.render` → `python.stitch.render`）。 |
| `scripts/run_stitch.sh` / `.ps1` | **新增。** 逐字转发到 `python -m python.stitch`。`.ps1` 用 `@args`，不再抄一遍 argparse。 |
| `scripts/run_python.sh` | 删 `uw-*` 五条子命令与其三个模块变量。其余（pool、`we-*`、`keypoint`、`oh-merge`、`label`）不动。 |
| `tests/python/test_stitch.py` | 原 `test_underwater.py` 改名 + import 改路径，33 个用例断言不动；追加新用例。 |
| `inputs/overhead/models/002.fbx` + `002.fbm/` | FBX 落位。 |
| `.gitignore` | 加 `inputs/overhead/models/`；config glob 泛化；注释更名。 |
| `README.md` | 「水下拼接」一节改写为「平面拼接（stitch）」，两条 profile 并列。 |

### 任务依赖

```
Task 1 (搬迁+改名，行为不变)
   ├── Task 2 (profiles.py)
   │      ├── Task 3 (render_video 去正则 + sync 分支)
   │      ├── Task 4 (export_ref_tex 双来源)
   │      └── Task 5 (run.py profile 化)
   │             └── Task 6 (dispatcher + shell 入口)
   │                    ├── Task 7 (FBX 落位 + .gitignore + overhead 离线验证)
   │                    └── Task 8 (overhead 实时链路)
   │                           └── Task 9 (README + 清理)
```

Task 1 必须最先做且单独提交：它是纯机械搬迁，把「改名」与「改行为」分开，任何回归都能
二分定位到后续任务。

---

## Task 1: 把 python/underwater 搬迁为 python/stitch（纯改名，行为不变）

**Files:**
- Rename: `python/underwater/` → `python/stitch/`（`git mv`，保留 6 个文件的历史）
- Rename: `tests/python/test_underwater.py` → `tests/python/test_stitch.py`
- Modify: `python/stitch/__init__.py`（文档串）
- Modify: `python/stitch/run.py:22`（`from python.underwater import render_video as RV`）
- Modify: `python/stitch/run.py:105`（`"-m", "python.underwater.extract"`）
- Modify: `python/stitch/render_video.py:38`（`from python.underwater import render as R`）
- Modify: `python/assets/compile_runtime_asset.py:13`（`from python.underwater.render import`）
- Modify: `tests/python/test_stitch.py`（24 处 `python.underwater.` → `python.stitch.`）
- Modify: `scripts/run_python.sh:165,171,182,194,211`（5 处 `python.underwater.` 模块名）
- Modify: `scripts/run_underwater.sh:37`、`scripts/run_underwater.ps1:40`（各 1 处模块名）

**Interfaces:**
- Consumes: 无（首个任务）。
- Produces: 包 `python.stitch`，导出与原 `python.underwater` **完全相同**的模块与符号：
  `extract.extract_to_json(src, dst, tex_dir, planes_only=False) -> list[dict]`、
  `extract.sort_meshes_by_world_x(meshes) -> list[dict]`、
  `extract.select_pool_planes(meshes, band=(-11.6,-8.0), min_height=2.5) -> list[dict]`、
  `render.render_stills(...) -> tuple[int, int]`、`render.resolve_ppm(xmin, xmax, target_width) -> float`、
  `render.build_remap_clipped(...)`、`render.seam_weights(masks, blend_px)`、
  `render.bottom_dirty_rows(coverage) -> int`、`render.crop_bottom_and_scale(image, crop_px, target_height, interpolation=cv2.INTER_LINEAR)`、
  `render_video.render_video(...) -> tuple[int,int,int]`、`render_video.load_manifest(video_dir)`、
  `render_video.alignment_plan(align_start_ms, align_end_ms, fps, cams, order)`、
  `render_video.camera_of(texture_basename)`（本任务仍保留，Task 3 才删）、
  `run.write_config(path, video_dir, backend, encode_path, align=True)`、`run.StepError`、
  `run.newer_than(target, *sources)`、`run.default_backend()`、`run.build_dir_for(backend)`、
  `run.executable_for(build_dir)`、`export_real_tex.export(out_dir, cams=None)`。

- [ ] **Step 1: 记录基线测试数**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 180 tests in ...s
OK
```
把 `180` 记下来，后续每步都要对比。

- [ ] **Step 2: git mv 目录与测试文件**

```bash
git mv python/underwater python/stitch
git mv tests/python/test_underwater.py tests/python/test_stitch.py
```

- [ ] **Step 3: 确认此刻测试是失败的（import 已断）**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED|ERROR)" | head -3
```
Expected: `FAILED (errors=...)` —— `test_stitch.py` 与 `compile_runtime_asset.py` 仍在 import
`python.underwater`，模块已不存在。这一步是为了确认接下来的改动确实有效，不是空转。

- [ ] **Step 4: 改 `python/stitch/__init__.py`**

整个文件替换为：

```python
"""Plane-stitch tasks: FBX extraction and N-plane horizontal composite.

One module set drives every stitch line; the per-line differences (model,
camera ids, pixel density, seam width, time-alignment policy) live as data in
profiles.py. Geometry and blending are reused from the pool pipeline
(python.assets.extract_fbx, python.validation.reference_renderer) rather than
copied.
"""
```

- [ ] **Step 5: 改 `python/stitch/run.py` 的两处引用**

`python/stitch/run.py:22`：

```python
from python.stitch import render_video as RV
```

`python/stitch/run.py:105`（`step_extract` 内）：

```python
    run([python_bin(), "-m", "python.stitch.extract", fbx, MESH_JSON,
         "--tex-dir", tex_dir, "--planes-only"])
```

- [ ] **Step 6: 改 `python/stitch/render_video.py:38`**

```python
from python.stitch import render as R
```

同时把模块文档串首行（`render_video.py:4`）里的 `python.underwater.render` 改为
`python.stitch.render`：

```python
Reuses the geometry + seam blending already validated for the still stitch
(python.stitch.render): same mesh JSON, same build_remap_clipped, same
```

- [ ] **Step 7: 改 `python/stitch/extract.py` 与 `render.py` 的文档串**

`python/stitch/extract.py:2` 的模块串首行改为：

```python
"""Extract a stitch FBX into pool-compatible mesh JSON, ordered left-to-right.

Reuses python.assets.extract_fbx for all FBX/UV/geometry logic; this module only
adds stitch-specific defaults, left-to-right ordering, correct texture
selection for multi-material meshes, and an isolated CLI.
"""
```

`python/stitch/render.py:1-6` 的模块串改为：

```python
"""Render a plane stitch from a mesh JSON: still + grid diagnostic.

Reuses python.validation.reference_renderer for all remap/feather/composite/grid
logic; this module only adds the stitch defaults, width-adaptive ppm, and an
isolated CLI.
"""
```

- [ ] **Step 8: 改 `python/assets/compile_runtime_asset.py:13`**

```python
from python.stitch.render import (
    bottom_dirty_rows,
    build_remap_clipped,
    seam_weights,
)
```

同时 `compile_runtime_asset.py:272` 的帮助文本：

```python
        help="bake hard vertical seams with this transition width instead of the "
        "pool's distance feather; matches python.stitch.render --blend-px",
```

- [ ] **Step 9: 改 `tests/python/test_stitch.py` 的 24 处 import**

用一条 `sed` 完成（这些 import 散在各测试方法内部，逐个手改容易漏）：

```bash
sed -i '' 's/python\.underwater\./python.stitch./g' tests/python/test_stitch.py
grep -c 'python\.stitch\.' tests/python/test_stitch.py
grep -c 'python\.underwater' tests/python/test_stitch.py || true
```
Expected: 第一个 `grep -c` 输出 `24`，第二个输出 `0`。

- [ ] **Step 10: 改三个 shell 脚本里的模块名**

```bash
sed -i '' 's/python\.underwater\./python.stitch./g' \
  scripts/run_python.sh scripts/run_underwater.sh scripts/run_underwater.ps1
grep -rn 'python\.underwater' scripts/ python/ tests/ || echo "no stale references"
```
Expected: `no stale references`

（`run_underwater.*` 在 Task 6 才退役；这里先让它们保持可用，避免中间态出现坏脚本。）

- [ ] **Step 11: 跑全量测试，确认回到基线**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 180 tests in ...s
OK
```
用例数必须仍是 180 —— 这一步只改名，不改行为。

- [ ] **Step 12: 冒烟测试：水下静图产物逐像素不变**

Run:
```bash
.venv/bin/python -m python.stitch.render \
  --data outputs/underwater/all_mesh.json \
  --tex-dir outputs/underwater/real_tex_all \
  --still /tmp/stitch_rename_check.png \
  --full-res --blend-px 120
.venv/bin/python -c "
import cv2, numpy as np
a = cv2.imread('outputs/underwater/all_real_stitch_bp120.png')
b = cv2.imread('/tmp/stitch_rename_check.png')
assert a is not None and b is not None, 'missing image'
assert a.shape == b.shape, f'shape {a.shape} != {b.shape}'
assert np.array_equal(a, b), 'pixels differ'
print('identical', a.shape)
"
```
Expected: `identical (360, 3278, 3)`

若 `outputs/underwater/real_tex_all/` 不存在，先跑
`.venv/bin/python -m python.stitch.export_real_tex`。

- [ ] **Step 13: 清理临时文件并提交**

```bash
rm -f /tmp/stitch_rename_check.png
git add -A python/ tests/ scripts/
git commit -F - <<'EOF'
refactor(stitch): rename python/underwater to python/stitch

The directory is about to hold an overhead-camera line as well, so the name
would be lying. Nothing else changes here: same modules, same symbols, same
180 tests, and the underwater still renders pixel-identical to the committed
reference. Keeping the rename in its own commit means any regression from the
profile work that follows can be bisected to a behavioural change rather than
a path change.
EOF
```

- [ ] **Step 14: 确认提交只含改名**

Run:
```bash
git show --stat HEAD | head -20
```
Expected: 看到 `python/underwater/... => python/stitch/...` 的 rename 记录（6 个）
与 `test_underwater.py => test_stitch.py`，另有 `compile_runtime_asset.py`、
三个 shell 脚本的小改动。**不应**出现任何新文件。

---

## Task 2: profiles.py —— 把两条线路的差异变成数据

**Files:**
- Create: `python/stitch/profiles.py`
- Test: `tests/python/test_stitch.py`（追加 `ProfileTest` 类）

**Interfaces:**
- Consumes: Task 1 的 `python.stitch` 包。
- Produces:
  - `Profile` —— `@dataclass(frozen=True)`，字段与类型见下方 Step 3 代码。
  - `StepError(RuntimeError)` —— 从 `run.py` 上移到此处，供两处共用（理由见 Step 3 注释）。
  - `PROFILES: dict[str, Profile]` —— 键为 `"underwater"` / `"overhead"`。
  - `get(name: str) -> Profile` —— 未注册名抛 `SystemExit`，消息含全部已注册名。
  - `names() -> list[str]` —— 已注册名，注册顺序。
  - `grid_dir() -> Path` —— 水下静图贴图目录，env 覆盖链
    `STITCH_GRID_DIR` → `ANNOTATION_PREVIEW_DATASET_ROOT`/annotation-grids → 本机默认。
  - `Profile` 的派生属性：`mesh_json`、`ref_tex_dir`、`metrics`（均 `Path`）；
    方法 `config_path(backend) -> Path`、`clip_for(video_dir, camera) -> Path`（缺失或
    歧义均抛 `StepError`）。
  - 模块常量 `PROJECT_ROOT`、`INPUTS`、`OUTPUTS`、`CONFIGS`、`GENERATED`（`Path`）。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/python/test_stitch.py` 末尾：

```python
class ProfileTest(unittest.TestCase):
    """A profile is the single place a stitch line's differences live."""

    def test_registry_holds_both_lines(self):
        from python.stitch import profiles

        self.assertEqual(profiles.names(), ["underwater", "overhead"])

    def test_underwater_values_match_the_shipped_pipeline(self):
        # These are the numbers the committed underwater artefacts were made
        # with; a profile that drifts from them silently changes the bake.
        from python.stitch import profiles

        p = profiles.get("underwater")
        self.assertEqual(p.camera_ids, tuple(f"underA{i}" for i in range(16, 0, -1)))
        self.assertEqual(p.clip_suffix, ".ts")
        self.assertEqual(p.ppm, 240.0)
        self.assertEqual(p.blend_px, 120.0)
        self.assertTrue(p.full_res)
        self.assertEqual(p.crop_bottom, "auto")
        self.assertTrue(p.clip_uv)
        self.assertTrue(p.planes_only)
        self.assertEqual(p.sync, "manifest")
        self.assertEqual(p.source_size, (1280, 720))
        self.assertEqual(p.ref_tex, "snapshot")
        self.assertEqual(p.asset.name, "underwater.swasset")

    def test_overhead_values_match_the_design(self):
        from python.stitch import profiles

        p = profiles.get("overhead")
        self.assertEqual(p.camera_ids, ("cam5", "cam6"))
        self.assertEqual(p.clip_suffix, ".mp4")
        self.assertEqual(p.ppm, 170.0)
        self.assertEqual(p.blend_px, 85.0)
        self.assertFalse(p.full_res)
        self.assertEqual(p.crop_bottom, "none")
        self.assertTrue(p.clip_uv)
        self.assertFalse(p.planes_only)
        self.assertEqual(p.sync, "none")
        self.assertEqual(p.source_size, (3840, 2160))
        self.assertEqual(p.ref_tex, "video")
        self.assertEqual(p.fbx.name, "002.fbx")
        self.assertEqual(p.asset.name, "overhead.swasset")

    def test_unknown_name_lists_the_registered_ones(self):
        from python.stitch import profiles

        with self.assertRaises(SystemExit) as caught:
            profiles.get("pool")
        message = str(caught.exception)
        self.assertIn("pool", message)
        self.assertIn("underwater", message)
        self.assertIn("overhead", message)

    def test_profile_is_immutable(self):
        import dataclasses
        from python.stitch import profiles

        with self.assertRaises(dataclasses.FrozenInstanceError):
            profiles.get("overhead").ppm = 1.0

    def test_overhead_still_tex_dir_is_the_designer_fbm(self):
        # underwater renders stills from the dataset's annotation-grids, not the
        # grids baked into the .fbm; overhead has no such split.
        from python.stitch import profiles

        p = profiles.get("overhead")
        self.assertEqual(p.still_tex_dir, p.tex_dir)
        self.assertEqual(p.tex_dir.name, "002.fbm")

    def test_grid_dir_honours_the_explicit_override(self):
        import os
        from unittest.mock import patch
        from python.stitch import profiles

        with patch.dict(os.environ, {"STITCH_GRID_DIR": "/tmp/grids-xyz"}):
            self.assertEqual(str(profiles.grid_dir()), "/tmp/grids-xyz")

    def test_grid_dir_falls_back_to_the_dataset_root(self):
        import os
        from unittest.mock import patch
        from python.stitch import profiles

        env = {"ANNOTATION_PREVIEW_DATASET_ROOT": "/tmp/ds-xyz"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("STITCH_GRID_DIR", None)
            self.assertEqual(str(profiles.grid_dir()),
                             "/tmp/ds-xyz/annotation-grids")

    def test_every_profile_has_a_distinct_out_dir_and_asset(self):
        from python.stitch import profiles

        all_profiles = [profiles.get(name) for name in profiles.names()]
        out_dirs = [p.out_dir for p in all_profiles]
        assets = [p.asset for p in all_profiles]
        self.assertEqual(len(set(out_dirs)), len(out_dirs))
        self.assertEqual(len(set(assets)), len(assets))

    def test_clip_for_matches_the_profile_suffix(self):
        import tempfile
        from python.stitch import profiles

        overhead = profiles.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "20260629_172532_cam5.mp4").write_bytes(b"")
            (td / "20260629_172532_cam5.ts").write_bytes(b"")   # wrong suffix
            found = overhead.clip_for(td, "cam5")
            self.assertEqual(found.name, "20260629_172532_cam5.mp4")

    def test_clip_for_reports_a_missing_clip(self):
        import tempfile
        from python.stitch import profiles

        overhead = profiles.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(profiles.StepError):
                overhead.clip_for(Path(td), "cam5")

    def test_clip_for_refuses_to_guess_between_two_matches(self):
        import tempfile
        from python.stitch import profiles

        overhead = profiles.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "a_cam5.mp4").write_bytes(b"")
            (td / "b_cam5.mp4").write_bytes(b"")
            with self.assertRaises(profiles.StepError):
                overhead.clip_for(td, "cam5")
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.ProfileTest -v 2>&1 | tail -5
```
Expected: `ModuleNotFoundError: No module named 'python.stitch.profiles'`

- [ ] **Step 3: 写 `python/stitch/profiles.py`**

```python
"""One record per stitch line: everything that differs between them.

A stitch line is "N planes standing side by side across one lane": the meshes
sort left-to-right by world X, neighbours meet at a hard vertical seam, and the
world is already upright. The underwater 16-plane panorama and the overhead
2-plane lane are both instances; the six-camera pool is NOT — it sits in two
rows, is not ordered by world X, and blends by distance transform. Adding pool
here would grow fields that serve exactly one line, so it keeps its own path
(python.validation.reference_renderer + the CMake pool_4k.swasset rule).

Adding a third line means adding a record here and nothing else.
"""
import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INPUTS = PROJECT_ROOT / "inputs"
OUTPUTS = PROJECT_ROOT / "outputs"
CONFIGS = INPUTS / "configs"
GENERATED = PROJECT_ROOT / "build" / "assets" / "generated"

_DEFAULT_DATASET = ("/Users/penghaotian/Downloads/DATAS/SWIMMING/"
                    "swimming-xlj-middle-20260708")


class StepError(RuntimeError):
    """A pipeline step failed; the message is already user-facing.

    Lives here rather than in run.py because clip lookup — the first thing that
    can fail for a caller-supplied directory — is a profile method, and run.py
    importing profiles is the right direction of dependency. run.py re-exports
    the name so its own callers and tests keep working.
    """


def grid_dir():
    """Where the underwater still renderer reads its grid textures.

    The canonical grid renders live in the dataset, not in all.fbm — the .fbm
    copies are stale. Overridable directly (STITCH_GRID_DIR) or via the dataset
    root the annotation_preview tools already use."""
    explicit = os.environ.get("STITCH_GRID_DIR")
    if explicit:
        return Path(explicit)
    dataset = os.environ.get("ANNOTATION_PREVIEW_DATASET_ROOT", _DEFAULT_DATASET)
    return Path(dataset) / "annotation-grids"


@dataclass(frozen=True)
class Profile:
    """The differences between one stitch line and another.

    Two pairs of fields look redundant but are not:

    `tex_dir` vs `still_tex_dir` — the first resolves texture basenames while
    reading the FBX (always the .fbm beside it); the second is what the still
    renderer actually reads. Underwater splits them (dataset grids beat the
    stale .fbm copies); overhead does not (the designer's calibration frames
    are in the .fbm).

    `ppm` vs `full_res` — ppm is the .swasset canvas density, which the runtime
    must honour exactly. full_res additionally rescales the *still* back to the
    source image height, so a human sees pixels at native scale. Underwater
    wants both (asset 6005x725, still 3278x360); overhead wants ppm only.
    """

    name: str
    fbx: Path
    tex_dir: Path
    still_tex_dir: Path
    camera_ids: tuple[str, ...]      # left-to-right, one per mesh in world-X order
    clip_suffix: str                 # ".ts" / ".mp4"
    ppm: float
    full_res: bool
    blend_px: float
    clip_uv: bool
    crop_bottom: str                 # "auto" | "none" | decimal string
    planes_only: bool
    sync: str                        # "manifest" | "none"
    source_size: tuple[int, int]
    ref_tex: str                     # "snapshot" | "video"
    out_dir: Path
    asset: Path

    @property
    def mesh_json(self):
        return self.out_dir / "mesh.json"

    @property
    def ref_tex_dir(self):
        return self.out_dir / "ref_tex"

    @property
    def metrics(self):
        return self.out_dir / "realtime.jsonl"

    def config_path(self, backend):
        return CONFIGS / f"{self.name}_{backend}.conf"

    def clip_for(self, video_dir, camera):
        """The one clip in `video_dir` belonging to `camera`.

        Both a missing and an ambiguous match are errors: silently picking one
        of two candidates would put the wrong camera on a plane, which shows up
        as a mis-registered seam far from here."""
        matches = sorted(Path(video_dir).glob(f"*_{camera}{self.clip_suffix}"))
        if not matches:
            raise StepError(
                f"no {self.clip_suffix} clip for {camera} in {video_dir}")
        if len(matches) > 1:
            raise StepError(f"ambiguous clips for {camera}: "
                            f"{[m.name for m in matches]}")
        return matches[0]


_UNDERWATER_MODELS = INPUTS / "underwater" / "models"
_OVERHEAD_MODELS = INPUTS / "overhead" / "models"

PROFILES = {
    "underwater": Profile(
        name="underwater",
        fbx=_UNDERWATER_MODELS / "all.fbx",
        tex_dir=_UNDERWATER_MODELS / "all.fbm",
        still_tex_dir=grid_dir(),
        camera_ids=tuple(f"underA{index}" for index in range(16, 0, -1)),
        clip_suffix=".ts",
        ppm=240.0,
        full_res=True,
        blend_px=120.0,
        clip_uv=True,
        crop_bottom="auto",
        planes_only=True,
        sync="manifest",
        source_size=(1280, 720),
        ref_tex="snapshot",
        out_dir=OUTPUTS / "underwater",
        asset=GENERATED / "underwater.swasset",
    ),
    "overhead": Profile(
        name="overhead",
        fbx=_OVERHEAD_MODELS / "002.fbx",
        tex_dir=_OVERHEAD_MODELS / "002.fbm",
        still_tex_dir=_OVERHEAD_MODELS / "002.fbm",
        camera_ids=("cam5", "cam6"),
        clip_suffix=".mp4",
        # 170 sits just above the 152~169 px/m the source frames actually carry
        # (measured from the UV<->world affine), so nothing is upscaled.
        ppm=170.0,
        # ppm is already native, so there is nothing to rescale a still back to.
        full_res=False,
        # 85px @170ppm is 0.5m, the same physical width as underwater's 120px
        # @240ppm; the two planes overlap 425px so it fits.
        blend_px=85.0,
        clip_uv=True,
        # Both planes are full height: the measured ragged tail is 2 rows, which
        # is the renderer's own margin padding, not a perspective floor gap.
        crop_bottom="none",
        # 002.fbx carries exactly the two planes, no rigging or lane strips —
        # and the filter is not merely unnecessary here, it is wrong:
        # select_pool_planes keeps meshes whose world-Y sits inside the pool band
        # (-11.6, -8.0) where the underwater planes are, while this overhead
        # model spans Y [20.47, 23.47]. Turning it on drops both planes.
        planes_only=False,
        # The 4K session has no usable wall clock: sync_summary reports
        # waiting_for_syncbridge_events and every mapping offset_us is null. The
        # six ZCAMs share one EzLink/IEEE1588 domain and one recording session,
        # so the residual skew is frame-level.
        sync="none",
        source_size=(3840, 2160),
        ref_tex="video",
        out_dir=OUTPUTS / "overhead",
        asset=GENERATED / "overhead.swasset",
    ),
}


def names():
    return list(PROFILES)


def get(name):
    """The profile called `name`, or exit naming the ones that exist."""
    profile = PROFILES.get(name)
    if profile is None:
        raise SystemExit(
            f"unknown profile: {name}; valid: {', '.join(names())}")
    return profile
```

- [ ] **Step 4: 运行测试确认通过**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.ProfileTest -v 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 12 tests in ...s
OK
```

- [ ] **Step 5: 跑全量测试**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 192 tests in ...s
OK
```
180 基线 + 12 个新用例。

- [ ] **Step 6: 手工核对 profile 与现状是否一致**

Run:
```bash
.venv/bin/python -c "
from python.stitch import profiles
for name in profiles.names():
    p = profiles.get(name)
    print(f'{name:11s} ppm={p.ppm:6.1f} blend={p.blend_px:5.1f} full_res={p.full_res!s:5s} '
          f'crop={p.crop_bottom:4s} sync={p.sync:8s} lanes={len(p.camera_ids):2d} '
          f'src={p.source_size}')
    print(f'{\"\":11s} mesh={p.mesh_json}')
    print(f'{\"\":11s} asset={p.asset}')
"
```
Expected:
```
underwater  ppm= 240.0 blend=120.0 full_res=True  crop=auto sync=manifest lanes=16 src=(1280, 720)
            mesh=/Users/.../outputs/underwater/mesh.json
            asset=/Users/.../build/assets/generated/underwater.swasset
overhead    ppm= 170.0 blend= 85.0 full_res=False crop=none sync=none     lanes= 2 src=(3840, 2160)
            mesh=/Users/.../outputs/overhead/mesh.json
            asset=/Users/.../build/assets/generated/overhead.swasset
```

- [ ] **Step 7: 提交**

```bash
git add python/stitch/profiles.py tests/python/test_stitch.py
git commit -F - <<'EOF'
feat(stitch): declare each line's differences as a profile record

The two stitch lines differ in fourteen values and nothing else — model,
texture dirs, camera ids, clip suffix, pixel density, seam width, crop policy,
time-alignment policy, source size. Those values are currently spread across
module constants in run.py, shell variables in run_python.sh, and CLI defaults
in three argparse blocks, so adding a line means editing all three.

The underwater record is pinned by assertions to the numbers the committed
artefacts were baked with: a profile that quietly drifted from ppm=240 or
crop_bottom=auto would change the bake without changing the mesh JSON.

Two field pairs carry a comment explaining why they are not redundant:
tex_dir/still_tex_dir (underwater reads dataset grids, not the stale .fbm
copies) and ppm/full_res (asset canvas density vs. rescaling a still back to
native source height).
EOF
```

---

## Task 3: render_video 按位置取相机，按 profile 决定对齐

**Files:**
- Modify: `python/stitch/render_video.py`（删 `camera_of` 与 `video_for_camera`；
  `render_video()` 签名加 `camera_ids` 与 `clip_for`；`align` 语义不变）
- Test: `tests/python/test_stitch.py`（改 `VideoAlignmentTest.test_camera_of_parses_texture_basename`，
  追加 `VideoCameraOrderTest`）

**Interfaces:**
- Consumes: Task 2 的 `profiles.get(name)`、`Profile.clip_for(video_dir, camera)`、
  `profiles.StepError`。
- Produces:
  - `render_video(data_path, video_dir, out_path, camera_ids, clip_for, seconds=None,
    ppm=None, unit_scale=1.0, neg_v=False, blend_px=0.0, full_res=True, align=True)
    -> tuple[int, int, int]` —— 返回 `(final_w, final_h, frames)`。`camera_ids` 是
    左→右有序相机名，长度必须等于 mesh 数；`clip_for` 是 `(video_dir, camera) -> Path`
    的可调用对象（生产环境传 `profile.clip_for`）。
  - `load_manifest` / `alignment_plan` 签名与行为不变（Task 1 已验证的 33 个用例继续覆盖）。
  - `camera_of` 与 `video_for_camera` **不再存在**。

- [ ] **Step 1: 写失败的测试**

先把 `tests/python/test_stitch.py` 里现有的
`VideoAlignmentTest.test_camera_of_parses_texture_basename` 整个方法**删掉**（它测的是
即将消失的函数）。

再在**模块层**（文件顶部现有 `_mesh` / `_band_plane` / `_strip` 旁边）加两个共用 helper
—— Task 4 与 Task 8 会直接调用它们，所以放模块层而不是某个类里：

```python
def _plane(node, tex, x0, width=1.0, y0=0.0, y1=1.0):
    """One quad, two triangles, UV spanning the full texture."""
    corners = [
        [{"pos": [x0, y0], "uv": [0.0, 0.0]},
         {"pos": [x0 + width, y0], "uv": [1.0, 0.0]},
         {"pos": [x0 + width, y1], "uv": [1.0, 1.0]}],
        [{"pos": [x0, y0], "uv": [0.0, 0.0]},
         {"pos": [x0 + width, y1], "uv": [1.0, 1.0]},
         {"pos": [x0, y1], "uv": [0.0, 1.0]}],
    ]
    return {"node": node, "texture_basename": tex, "uvset": "UVChannel_1",
            "const_axis": 2, "kept_axes": [0, 1], "spans": [width, y1 - y0, 0],
            "triangles": corners}


def _two_plane_json(td, planes):
    """Write a mesh JSON of `planes` (already built by _plane) and return it."""
    import json

    path = Path(td) / "mesh.json"
    path.write_text(json.dumps({"source": "test", "meshes": list(planes)}))
    return path
```

最后在文件末尾追加：

```python
class VideoCameraOrderTest(unittest.TestCase):
    """Camera identity comes from the profile's ordered ids, not from parsing a
    texture filename: the overhead textures are 05-02.jpg and C06.jpg, which no
    naming rule maps to cam5/cam6."""

    def test_camera_count_must_match_mesh_count(self):
        import tempfile
        from python.stitch.render_video import render_video

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            data = _two_plane_json(td, [_plane("a", "05-02.jpg", 0.0),
                                        _plane("b", "C06.jpg", 0.9)])
            with self.assertRaises(SystemExit) as caught:
                render_video(data, td, td / "out.mp4",
                             camera_ids=("cam5",),          # one id, two meshes
                             clip_for=lambda d, c: td / "absent.mp4")
            message = str(caught.exception)
            self.assertIn("1", message)
            self.assertIn("2", message)

    def test_clip_lookup_is_delegated_to_the_caller(self):
        # render_video must not glob for clips itself; the profile owns the
        # suffix and the ambiguity rules.
        import tempfile
        from python.stitch.render_video import render_video

        asked = []

        def fake_clip_for(video_dir, camera):
            asked.append(camera)
            raise RuntimeError("stop here")

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            data = _two_plane_json(td, [_plane("a", "05-02.jpg", 0.0),
                                        _plane("b", "C06.jpg", 0.9)])
            with self.assertRaises(RuntimeError):
                render_video(data, td, td / "out.mp4",
                             camera_ids=("cam5", "cam6"),
                             clip_for=fake_clip_for)
        self.assertEqual(asked, ["cam5"])

    def test_camera_of_is_gone(self):
        # The underA-only regex was the last thing tying the video path to the
        # underwater naming scheme.
        import python.stitch.render_video as rv

        self.assertFalse(hasattr(rv, "camera_of"))
        self.assertFalse(hasattr(rv, "video_for_camera"))
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.VideoCameraOrderTest -v 2>&1 | tail -8
```
Expected: 三个用例全部 FAIL/ERROR —— `render_video()` 还不接受 `camera_ids` 关键字
（`TypeError: render_video() got an unexpected keyword argument 'camera_ids'`），
且 `camera_of` 仍然存在。

- [ ] **Step 3: 删掉 `render_video.py` 的 `camera_of` 与 `video_for_camera`**

删除 `python/stitch/render_video.py` 的这两个函数（原第 44-57 行）：

```python
def camera_of(texture_basename):
    """`underA7-grid.png` -> `underA7`; None if it doesn't match."""
    m = re.match(r"(underA\d+)", texture_basename or "")
    return m.group(1) if m else None


def video_for_camera(video_dir, cam):
    """The single clip in `video_dir` whose name ends `_<cam>.ts`, else raise."""
    hits = sorted(video_dir.glob(f"*_{cam}.ts"))
    if not hits:
        raise SystemExit(f"no video for {cam} in {video_dir}")
    if len(hits) > 1:
        raise SystemExit(f"ambiguous videos for {cam}: {[h.name for h in hits]}")
    return hits[0]
```

同时删掉现在没人用的 `import re`（`render_video.py:29`）。

- [ ] **Step 4: 改 `render_video()` 的签名与相机解析段**

`python/stitch/render_video.py` 的函数签名（原第 122-124 行）改为：

```python
def render_video(data_path, video_dir, out_path, camera_ids, clip_for,
                 seconds=None, ppm=None, unit_scale=1.0, neg_v=False,
                 blend_px=0.0, full_res=True, align=True):
```

把原来的相机解析段（原第 142-155 行）：

```python
    cam_order = []
    for m in meshes:
        cam = camera_of(m["texture_basename"])
        if cam is None:
            raise SystemExit(f"cannot derive camera from {m['texture_basename']}")
        cam_order.append(cam)

    caps, src_wh = [], []
    for cam in cam_order:
        cap = cv2.VideoCapture(str(video_for_camera(video_dir, cam)))
```

替换为：

```python
    # Camera identity is positional: extract sorts meshes left-to-right by world
    # X and the profile lists its ids in that same order. Deriving it from the
    # texture filename instead only ever worked for the underA* naming scheme —
    # the overhead textures are 05-02.jpg and C06.jpg.
    cam_order = list(camera_ids)
    if len(cam_order) != len(meshes):
        raise SystemExit(f"camera count mismatch: {len(cam_order)} ids for "
                         f"{len(meshes)} meshes in {data_path}")

    caps, src_wh = [], []
    for cam in cam_order:
        cap = cv2.VideoCapture(str(clip_for(video_dir, cam)))
```

- [ ] **Step 5: 改模块文档串里的相机映射说明**

`render_video.py:10-11` 现在写的是贴图名反解规则，改为：

```python
Camera↔plane mapping is positional: extract orders meshes left-to-right by world
X, and the caller passes `camera_ids` in that same order plus a `clip_for`
lookup (python.stitch.profiles.Profile supplies both).
```

- [ ] **Step 6: 改 `render_video.py` 的 CLI，让它自己查 profile**

把 `main()`（原第 306-331 行）整个替换为：

```python
def main(argv=None):
    ap = argparse.ArgumentParser(description="Stitch plane textures from video")
    ap.add_argument("video_dir", type=Path,
                    help="directory holding one clip per camera")
    ap.add_argument("--profile", default="underwater",
                    help="stitch line whose camera ids and clip suffix to use "
                         "(default: %(default)s)")
    ap.add_argument("--data", type=Path, default=None,
                    help="mesh JSON (default: the profile's)")
    ap.add_argument("--out", type=Path, default=None,
                    help="output mp4 (default: <profile out_dir>/stitch.mp4)")
    ap.add_argument("--seconds", type=float, default=None,
                    help="cap output duration; default uses the whole align window")
    ap.add_argument("--ppm", type=float, default=None,
                    help="pixels per metre; default adapts to source height in --full-res")
    ap.add_argument("--unit-scale", type=float, default=1.0)
    ap.add_argument("--neg-v", dest="neg_v", action="store_true", default=False)
    ap.add_argument("--blend-px", type=float, default=None,
                    help="horizontal pixels blended across each vertical seam "
                         "(default: the profile's)")
    ap.add_argument("--no-full-res", action="store_true",
                    help="skip source-height rescale / bottom auto-crop")
    ap.add_argument("--no-align", action="store_true",
                    help="ignore manifest wall clocks and read every clip from "
                         "frame 0; already implied for profiles whose "
                         "recordings carry no wall clock")
    args = ap.parse_args(argv)

    profile = P.get(args.profile)
    # A profile whose recordings have no usable wall clock (sync="none") reads
    # from frame 0 anyway; --no-align forces that for a manifest-bearing one.
    align = profile.sync == "manifest" and not args.no_align
    render_video(
        args.data or profile.mesh_json,
        args.video_dir,
        args.out or profile.out_dir / "stitch.mp4",
        camera_ids=profile.camera_ids,
        clip_for=profile.clip_for,
        seconds=args.seconds,
        ppm=args.ppm if args.ppm is not None else profile.ppm,
        unit_scale=args.unit_scale,
        neg_v=args.neg_v,
        blend_px=args.blend_px if args.blend_px is not None else profile.blend_px,
        full_res=profile.full_res and not args.no_full_res,
        align=align,
    )
```

并在 `render_video.py` 的 import 段（原第 37-38 行附近）加：

```python
from python.stitch import profiles as P
```

- [ ] **Step 7: 运行新测试确认通过**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.VideoCameraOrderTest -v 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 3 tests in ...s
OK
```

- [ ] **Step 8: 跑全量测试**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 194 tests in ...s
OK
```
192（Task 2 后）− 1（删掉的 `camera_of` 用例）+ 3（新增）= 194。

- [ ] **Step 9: 冒烟测试 —— 水下离线视频仍按 manifest 对齐**

Run:
```bash
.venv/bin/python -m python.stitch.render_video \
  /Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-xlj-under-videos/swb_20260728-150356_6 \
  --profile underwater \
  --data outputs/underwater/all_mesh.json \
  --out /tmp/uw_align_check.mp4 \
  --seconds 2 2>&1 | grep -E "canvas|align window|skew"
```
Expected（数值可略有不同，但三行都必须出现）：
```
canvas 6005x725 @ 240.00px/m -> output 3278x360 (bottom crop 68px)
wall-clock align window 30.000s @ 30fps (sync_mode=manifest align_start/align_end)
per-camera keyframe skew ...ms -> start frames ...
```
关键是 `align window` 那行仍在 —— profile 化不能让水下悄悄退化成不对齐。

- [ ] **Step 10: 清理并提交**

```bash
rm -f /tmp/uw_align_check.mp4
git add python/stitch/render_video.py tests/python/test_stitch.py
git commit -F - <<'EOF'
refactor(stitch): take camera identity from the profile, not the texture name

camera_of() parsed `underA\d+` out of a texture basename, so it returned None
for the overhead textures (05-02.jpg and C06.jpg) and killed the render. No
naming rule maps those to cam5/cam6 — the mapping is positional, and both ends
already agree on the order: extract sorts meshes by world X ascending and the
profile lists its ids left-to-right. So the regex was not just underwater-only,
it was redundant with an ordering the pipeline already guarantees.

Clip lookup moves out too. render_video no longer globs; the profile owns the
suffix and the missing/ambiguous rules, which is also what the realtime config
writer needs, so there is now one implementation instead of two.

--no-align keeps working, but a profile whose recordings carry no wall clock
now defaults to it instead of requiring the caller to remember.
EOF
```

---

## Task 4: 参考贴图按相机 ID 命名，两种来源

**Files:**
- Rename: `python/stitch/export_real_tex.py` → `python/stitch/export_ref_tex.py`（`git mv`）
- Modify: `python/stitch/export_ref_tex.py`（重写：两种来源、按 `camera_id` 命名）
- Modify: `python/stitch/render.py`（`render_stills` 加 `tex_names` 参数）
- Modify: `scripts/run_python.sh:171`（`export_real_tex` → `export_ref_tex`；Task 6 才删整段）
- Test: `tests/python/test_stitch.py`（追加 `RefTexTest`、`RenderTexNamesTest`）

**Interfaces:**
- Consumes: Task 2 的 `profiles.get(name)`、`Profile.camera_ids`、`Profile.ref_tex`、
  `Profile.ref_tex_dir`、`Profile.clip_for`、`profiles.StepError`。
- Produces:
  - `export_ref_tex.export(profile, out_dir=None, video_dir=None) -> list[Path]` ——
    按 `profile.ref_tex` 选来源，每台相机写一个 `<camera_id>.png`，返回写出的路径
    （顺序与 `profile.camera_ids` 一致）。`ref_tex="video"` 而未给 `video_dir` 时抛
    `StepError`。
  - `export_ref_tex.tex_names(profile) -> list[str]` —— `[f"{cam}.png" for cam in
    profile.camera_ids]`，供 `render` 与测试共用，避免两处各拼一次字符串。
  - `export_ref_tex.first_frame(path) -> numpy.ndarray` —— 读视频第 0 帧，读不出抛
    `StepError`。
  - `render.render_stills(..., tex_names=None)` —— `None` 时按 `texture_basename`
    读贴图（现有行为，逐像素不变）；给列表时按位置取名，长度必须等于 mesh 数。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/python/test_stitch.py` 末尾：

```python
class RefTexTest(unittest.TestCase):
    """Reference textures are named after the camera, not after the mesh's
    texture basename: the overhead basenames (05-02.jpg, C06.jpg) say nothing
    about which camera they came from, and reusing a .jpg name would re-encode
    a lossless frame as JPEG."""

    def test_tex_names_follow_camera_ids(self):
        from python.stitch import export_ref_tex, profiles

        self.assertEqual(export_ref_tex.tex_names(profiles.get("overhead")),
                         ["cam5.png", "cam6.png"])
        names = export_ref_tex.tex_names(profiles.get("underwater"))
        self.assertEqual(names[0], "underA16.png")
        self.assertEqual(names[-1], "underA1.png")
        self.assertEqual(len(names), 16)

    def test_video_source_requires_a_video_dir(self):
        from python.stitch import export_ref_tex, profiles

        with self.assertRaises(profiles.StepError):
            export_ref_tex.export(profiles.get("overhead"), video_dir=None)

    def test_video_source_writes_one_png_per_camera(self):
        import tempfile
        import cv2
        import numpy as np
        from python.stitch import export_ref_tex, profiles

        overhead = profiles.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            clips = td / "clips"
            clips.mkdir()
            # two one-frame mp4s, distinguishable by colour
            for index, camera in enumerate(overhead.camera_ids):
                frame = np.full((16, 32, 3), 40 * (index + 1), np.uint8)
                writer = cv2.VideoWriter(
                    str(clips / f"sess_{camera}.mp4"),
                    cv2.VideoWriter_fourcc(*"mp4v"), 30.0, (32, 16))
                writer.write(frame)
                writer.release()

            out = td / "ref_tex"
            written = export_ref_tex.export(overhead, out_dir=out, video_dir=clips)

            self.assertEqual([p.name for p in written], ["cam5.png", "cam6.png"])
            for path in written:
                self.assertTrue(path.is_file())
                self.assertEqual(cv2.imread(str(path)).shape, (16, 32, 3))

    def test_unreadable_clip_is_reported(self):
        # OpenCV prints "moov atom not found" to stderr here; the point is that
        # export raises instead of writing a black frame.
        import tempfile
        from python.stitch import export_ref_tex, profiles

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for camera in profiles.get("overhead").camera_ids:
                (td / f"sess_{camera}.mp4").write_bytes(b"not a video")
            with self.assertRaises(profiles.StepError):
                export_ref_tex.export(profiles.get("overhead"),
                                      out_dir=td / "out", video_dir=td)


class RenderTexNamesTest(unittest.TestCase):
    """render_stills reads texture_basename by default and positional names when
    asked, so one renderer serves both the designer's calibration frames and the
    camera-named reference exports."""

    def test_positional_names_render_the_same_as_basenames(self):
        import tempfile
        import cv2
        import numpy as np
        from python.stitch.render import render_stills

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            data = _two_plane_json(td, [_plane("a", "05-02.jpg", 0.0),
                                        _plane("b", "C06.jpg", 0.9)])
            left = np.full((16, 32, 3), 90, np.uint8)
            right = np.full((16, 32, 3), 180, np.uint8)
            # same pixels under both naming schemes, both lossless
            cv2.imwrite(str(td / "05-02.jpg"), left)
            cv2.imwrite(str(td / "C06.jpg"), right)
            cv2.imwrite(str(td / "cam5.png"), left)
            cv2.imwrite(str(td / "cam6.png"), right)

            by_basename = td / "a.png"
            by_position = td / "b.png"
            render_stills(data, td, by_basename, None, ppm=64.0)
            render_stills(data, td, by_position, None, ppm=64.0,
                          tex_names=["cam5.png", "cam6.png"])

            self.assertTrue(np.array_equal(cv2.imread(str(by_basename)),
                                           cv2.imread(str(by_position))))

    def test_tex_names_length_must_match_mesh_count(self):
        import tempfile
        from python.stitch.render import render_stills

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            data = _two_plane_json(td, [_plane("a", "05-02.jpg", 0.0),
                                        _plane("b", "C06.jpg", 0.9)])
            with self.assertRaises(SystemExit) as caught:
                render_stills(data, td, td / "out.png", None, ppm=64.0,
                              tex_names=["cam5.png"])
            self.assertIn("1", str(caught.exception))
            self.assertIn("2", str(caught.exception))
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.RefTexTest \
  tests.python.test_stitch.RenderTexNamesTest -v 2>&1 | tail -6
```
Expected: `ModuleNotFoundError: No module named 'python.stitch.export_ref_tex'`

- [ ] **Step 3: `git mv` 并重写 `export_ref_tex.py`**

```bash
git mv python/stitch/export_real_tex.py python/stitch/export_ref_tex.py
```

整个文件替换为：

```python
"""Export one reference texture per camera: the frame the stitch really sees.

The textures a designer bakes into the .fbm carry calibration overlays (yellow
lane lines, distance labels), which is what you want when checking geometry and
not what you want when judging image quality. This writes each camera's first
captured frame instead, so `render --real` stitches photographic imagery by
swapping one directory.

Files are named `<camera_id>.png`, not after the mesh's texture_basename:
- the overhead basenames (05-02.jpg, C06.jpg) name a designer's working file,
  so a directory full of them says nothing about which camera is which;
- reusing a `.jpg` basename would re-encode a lossless decode as JPEG. Measured
  on cam5: max channel error 35, and the stitched result drifts by up to 22.

Two sources, chosen by profile.ref_tex:
- "snapshot": the dataset's per-camera snapshot index (annotation_preview), used
  by the underwater line whose cameras appear in 50 synchronised snapshots;
- "video": frame 0 of each clip in --video-dir, used by lines that only have
  recordings.
"""
import argparse
from pathlib import Path

import cv2

from python.annotation_preview import common as C
from python.stitch import profiles as P
from python.stitch.profiles import StepError


def tex_names(profile):
    """The basenames `export` writes, in profile camera order.

    Shared with the renderer so the two never disagree about the naming rule."""
    return [f"{camera}.png" for camera in profile.camera_ids]


def first_frame(path):
    """Frame 0 of `path` as BGR uint8; a clip we cannot decode is fatal."""
    capture = cv2.VideoCapture(str(path))
    try:
        ok, frame = capture.read()
    finally:
        capture.release()
    if not ok or frame is None:
        raise StepError(f"cannot read the first frame of {path}")
    return frame


def _snapshot_frame(camera):
    """Frame 0 for `camera` from the dataset snapshot index, or None.

    C.frames_for_camera returns [(snapshot_id, path)] in snapshot time order, so
    element 0 is that camera's earliest frame."""
    frames = C.frames_for_camera(camera)
    if not frames:
        return None
    image = cv2.imread(str(frames[0][1]))
    if image is None:
        raise StepError(f"cannot read snapshot frame for {camera}: {frames[0][1]}")
    return image


def export(profile, out_dir=None, video_dir=None):
    """Write one `<camera_id>.png` per camera; return the paths in camera order."""
    out_dir = Path(out_dir) if out_dir is not None else profile.ref_tex_dir
    if profile.ref_tex == "video":
        if video_dir is None:
            raise StepError(
                f"profile {profile.name} takes reference textures from video; "
                "pass --video-dir")
    elif profile.ref_tex == "snapshot":
        if not Path(C.SNAP_DIR).is_dir():
            raise StepError(
                f"snapshot directory missing: {C.SNAP_DIR} "
                "(point ANNOTATION_PREVIEW_DATASET_ROOT at a valid dataset)")
    else:
        raise StepError(f"unknown ref_tex source: {profile.ref_tex!r}")

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for camera, name in zip(profile.camera_ids, tex_names(profile)):
        if profile.ref_tex == "video":
            source = profile.clip_for(video_dir, camera)
            image = first_frame(source)
        else:
            source = None
            image = _snapshot_frame(camera)
            if image is None:
                print(f"  {camera:9s} (no frames — skipped)")
                continue
        destination = out_dir / name
        if not cv2.imwrite(str(destination), image):
            raise StepError(f"cannot write {destination}")
        written.append(destination)
        origin = Path(source).name if source is not None else "snapshot"
        print(f"  {camera:9s} <- {origin}")
    if not written:
        raise StepError("no reference textures exported")
    print(f"wrote {len(written)} reference textures -> {out_dir}")
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export per-camera reference textures")
    ap.add_argument("--profile", default="underwater",
                    help="stitch line to export for (default: %(default)s)")
    ap.add_argument("--out-dir", type=Path, default=None,
                    help="destination (default: <profile out_dir>/ref_tex)")
    ap.add_argument("--video-dir", type=Path, default=None,
                    help="clip directory, required when the profile's reference "
                         "textures come from video")
    args = ap.parse_args(argv)
    profile = P.get(args.profile)
    try:
        export(profile, out_dir=args.out_dir, video_dir=args.video_dir)
    except StepError as error:
        raise SystemExit(f"error: {error}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 给 `render.py` 的 `render_stills` 加 `tex_names`**

`python/stitch/render.py` 的 `render_stills` 签名（原第 171-174 行）加一个参数：

```python
def render_stills(data_path, tex_dir, still_path, grid_path,
                  ppm=None, unit_scale=1.0, neg_v=False, target_width=640, margin=2,
                  blend_px=0.0, full_res=False, heatmap_path=None,
                  crop_bottom_px=0, tex_names=None):
```

把读贴图那一段（原第 185-191 行）：

```python
    texs = []
    for m in meshes:
        path = tex_dir / m["texture_basename"]
        texture = cv2.imread(str(path))
        if texture is None:
            raise SystemExit(f"cannot read texture: {path}")
        texs.append(texture)
```

替换为：

```python
    # By default each mesh names its own texture; `tex_names` overrides that
    # positionally, which is how the camera-named reference exports are read
    # (their filenames come from the profile, not from the FBX).
    if tex_names is None:
        names = [m["texture_basename"] for m in meshes]
    else:
        names = list(tex_names)
        if len(names) != len(meshes):
            raise SystemExit(f"texture count mismatch: {len(names)} names for "
                             f"{len(meshes)} meshes in {data_path}")
    texs = []
    for name in names:
        path = tex_dir / name
        texture = cv2.imread(str(path))
        if texture is None:
            raise SystemExit(f"cannot read texture: {path}")
        texs.append(texture)
```

`render.py` 的 CLI 也加一个开关，让手动调用能用上导出的参考贴图：

```python
    ap.add_argument("--tex-names", nargs="+", default=None,
                    help="texture basenames in mesh order, overriding each "
                         "mesh's own texture_basename")
```

并在 `main()` 的 `render_stills(...)` 调用里加 `tex_names=args.tex_names`。

- [ ] **Step 5: 改 `scripts/run_python.sh:171` 的模块名**

```bash
sed -i '' 's/python\.stitch\.export_real_tex/python.stitch.export_ref_tex/' scripts/run_python.sh
grep -n 'export_re[af]' scripts/run_python.sh
```
Expected: 只剩一行 `"$PY" -m python.stitch.export_ref_tex "$@"`

（`uw-real` 仍指向旧的 `real_tex_all` 目录，Task 6 退役该段时一并处理。）

- [ ] **Step 6: 运行新测试确认通过**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.RefTexTest \
  tests.python.test_stitch.RenderTexNamesTest -v 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 6 tests in ...s
OK
```

- [ ] **Step 7: 跑全量测试**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 200 tests in ...s
OK
```
194（Task 3 后）+ 6 = 200。

- [ ] **Step 8: 冒烟测试 —— 水下参考贴图改名后静图仍一致**

`.png` 无损，改名不该改变任何像素：

```bash
.venv/bin/python -m python.stitch.export_ref_tex --profile underwater \
  --out-dir /tmp/uw_ref_check
.venv/bin/python -m python.stitch.render \
  --data outputs/underwater/all_mesh.json \
  --tex-dir /tmp/uw_ref_check \
  --tex-names underA16.png underA15.png underA14.png underA13.png \
              underA12.png underA11.png underA10.png underA9.png \
              underA8.png underA7.png underA6.png underA5.png \
              underA4.png underA3.png underA2.png underA1.png \
  --still /tmp/uw_ref_stitch.png --full-res --blend-px 120
.venv/bin/python -c "
import cv2, numpy as np
a = cv2.imread('outputs/underwater/all_real_stitch_bp120.png')
b = cv2.imread('/tmp/uw_ref_stitch.png')
assert a.shape == b.shape, f'{a.shape} != {b.shape}'
assert np.array_equal(a, b), f'max diff {cv2.absdiff(a,b).max()}'
print('identical', a.shape)
"
```
Expected: `identical (360, 3278, 3)`

- [ ] **Step 9: 冒烟测试 —— overhead 参考贴图来自视频**

```bash
.venv/bin/python -m python.stitch.export_ref_tex --profile overhead \
  --out-dir /tmp/oh_ref_check \
  --video-dir /Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K
.venv/bin/python -c "
import cv2
for name in ('cam5.png', 'cam6.png'):
    image = cv2.imread(f'/tmp/oh_ref_check/{name}')
    assert image is not None, name
    print(name, image.shape)
"
```
Expected:
```
  cam5      <- 20260629_172532_cam5.mp4
  cam6      <- 20260629_172532_cam6.mp4
wrote 2 reference textures -> /tmp/oh_ref_check
cam5.png (2160, 3840, 3)
cam6.png (2160, 3840, 3)
```

- [ ] **Step 10: 清理并提交**

```bash
rm -rf /tmp/uw_ref_check /tmp/oh_ref_check /tmp/uw_ref_stitch.png
git add python/stitch/ tests/python/test_stitch.py scripts/run_python.sh
git commit -F - <<'EOF'
feat(stitch): name reference textures after the camera, from either source

Reference exports used to reuse each mesh's texture_basename so the renderer
could find them by swapping one directory. That breaks down for overhead: the
basenames are 05-02.jpg and C06.jpg — a designer's working filenames, which
tell a reader nothing about which camera produced which frame — and writing a
lossless decode back under a .jpg name re-encodes it (measured max channel
error 35 on cam5, up to 22 in the stitched result).

So exports are now <camera_id>.png and the renderer takes an optional
positional tex_names list. Default behaviour is untouched: with tex_names=None
it still reads texture_basename, and the underwater still renders
pixel-identical to the committed reference.

The source is also per-profile now — the dataset snapshot index for underwater,
frame 0 of each clip for lines that only have recordings.
EOF
```

---

## Task 5: run.py 从 profile 取参，不再靠模块常量

**Files:**
- Modify: `python/stitch/run.py`（删 7 个模块常量；每个 `step_*` 与 `write_config`
  改为吃 `profile`；`ASSET_STAMP` 路径与内容带 profile 身份）
- Test: `tests/python/test_stitch.py`（改 `OneClickRunnerTest` 与
  `LaneAlignmentConfigTest` 的 4 处 `write_config` 调用，追加 `RunProfileTest`）

**Interfaces:**
- Consumes: Task 2 的 `profiles`（`get`、`StepError`、`CONFIGS`、`PROJECT_ROOT`）、
  Task 3 的 `render_video.load_manifest` / `alignment_plan`。
- Produces:
  - `write_config(profile, path, video_dir, backend, encode_path, align=True) -> None`
    —— 第一参数变为 `profile`（原先是 `path`）。
  - `lane_start_offsets(profile, video_dir) -> dict[str, int]` —— 加 `profile` 首参；
    `profile.sync != "manifest"` 时直接返回 `{}` 且不打印任何提示。
  - `asset_options(profile, args) -> tuple[list[str], str]` —— stamp 字符串以
    `profile.name` 开头。
  - `stamp_path(profile) -> Path` —— `profile.asset.with_suffix(".stamp")`。
  - `step_extract(profile, args)` / `step_asset(profile, args)` /
    `step_build(profile, args)` / `step_run(profile, args)` —— 全部加 `profile` 首参。
  - `StepError` 从 `profiles` 重新导出（`from python.stitch.profiles import StepError`），
    现有 `runner.StepError` 的引用继续可用。
  - 不再存在：`OUTPUTS`、`MODELS`、`MESH_JSON`、`ASSET`、`ASSET_STAMP`、`CAMERA_IDS`、
    `ASSET_PPM`、`ASSET_BLEND_PX`、`SOURCE_SIZE`。

- [ ] **Step 1: 改现有 4 处 `write_config` 调用（测试先适配新签名）**

`tests/python/test_stitch.py` 里 `OneClickRunnerTest` 与 `LaneAlignmentConfigTest`
共有 4 处调用 `runner.write_config(...)`。逐个改为传 profile：

`test_generated_config_declares_sixteen_lanes_right_to_left`：

```python
    def test_generated_config_declares_sixteen_lanes_right_to_left(self):
        import tempfile
        import python.stitch.run as runner
        from python.stitch import profiles

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for index in range(1, 17):
                (td / f"swb_test_underA{index}.ts").write_bytes(b"")
            config = td / "generated.conf"
            runner.write_config(profiles.get("underwater"), config, td, "metal",
                                td / "out.h265", align=False)

            lines = config.read_text().splitlines()
            sources = [line.split("=", 1)[0].removeprefix("source.")
                       for line in lines if line.startswith("source.")]
            # extract orders meshes left-to-right, which is underA16 -> underA1
            self.assertEqual(sources, [f"underA{i}" for i in range(16, 0, -1)])
            self.assertIn("backend=metal", lines)
```

`test_missing_clip_is_reported_not_silently_skipped`：

```python
    def test_missing_clip_is_reported_not_silently_skipped(self):
        import tempfile
        import python.stitch.run as runner
        from python.stitch import profiles

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for index in range(1, 16):          # underA16 absent
                (td / f"swb_test_underA{index}.ts").write_bytes(b"")
            with self.assertRaises(runner.StepError):
                runner.write_config(profiles.get("underwater"), td / "c.conf",
                                    td, "metal", td / "o.h265", align=False)
```

`test_start_offsets_come_from_the_manifest_skew` 不调 `write_config`，只需把
`runner.RV.alignment_plan` 保持原样 —— 该用例不改。

`test_config_omits_start_ms_when_alignment_is_disabled`：

```python
    def test_config_omits_start_ms_when_alignment_is_disabled(self):
        import tempfile
        import python.stitch.run as runner
        from python.stitch import profiles

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for index in range(1, 17):
                (td / f"swb_test_underA{index}.ts").write_bytes(b"")
            config = td / "c.conf"
            runner.write_config(profiles.get("underwater"), config, td, "metal",
                                td / "o.h265", align=False)
            self.assertNotIn("start_ms", config.read_text())
```

- [ ] **Step 2: 写新增的失败测试**

追加到 `tests/python/test_stitch.py` 末尾：

```python
class RunProfileTest(unittest.TestCase):
    """The runner reads every path and lane from the profile, so two lines can
    share it without either one's artefacts leaking into the other's."""

    def test_config_lanes_and_asset_come_from_the_profile(self):
        import tempfile
        import python.stitch.run as runner
        from python.stitch import profiles

        overhead = profiles.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            for camera in overhead.camera_ids:
                (td / f"20260629_172532_{camera}.mp4").write_bytes(b"")
            config = td / "overhead.conf"
            runner.write_config(overhead, config, td, "metal", td / "o.h265")

            lines = config.read_text().splitlines()
            sources = [line.split("=", 1)[0].removeprefix("source.")
                       for line in lines if line.startswith("source.")]
            self.assertEqual(sources, ["cam5", "cam6"])
            self.assertIn(f"asset={overhead.asset.as_posix()}", lines)
            self.assertIn(f"metrics={overhead.metrics.as_posix()}", lines)

    def test_sync_none_skips_the_manifest_entirely(self):
        # An overhead recording has no manifest at all; treating that as an
        # exception to report every run would be noise, not information.
        import tempfile
        import python.stitch.run as runner
        from python.stitch import profiles

        def explode(_video_dir):
            raise AssertionError("load_manifest must not be called")

        original = runner.RV.load_manifest
        try:
            runner.RV.load_manifest = explode
            with tempfile.TemporaryDirectory() as td:
                offsets = runner.lane_start_offsets(profiles.get("overhead"),
                                                    Path(td))
            self.assertEqual(offsets, {})
        finally:
            runner.RV.load_manifest = original

    def test_sync_manifest_still_reads_the_manifest(self):
        import tempfile
        import python.stitch.run as runner
        from python.stitch import profiles

        called = []

        def fake(video_dir):
            called.append(video_dir)
            raise SystemExit("no manifest here")

        original = runner.RV.load_manifest
        try:
            runner.RV.load_manifest = fake
            with tempfile.TemporaryDirectory() as td:
                offsets = runner.lane_start_offsets(profiles.get("underwater"),
                                                    Path(td))
            # a missing manifest degrades to "read from frame 0" for the
            # realtime path, but it must have been attempted
            self.assertEqual(offsets, {})
            self.assertEqual(len(called), 1)
        finally:
            runner.RV.load_manifest = original

    def test_asset_stamp_is_per_profile(self):
        import python.stitch.run as runner
        from python.stitch import profiles

        underwater = runner.stamp_path(profiles.get("underwater"))
        overhead = runner.stamp_path(profiles.get("overhead"))
        self.assertNotEqual(underwater, overhead)
        self.assertEqual(underwater.name, "underwater.stamp")
        self.assertEqual(overhead.name, "overhead.stamp")

    def test_asset_stamp_records_the_profile_and_its_shaping(self):
        import argparse
        import python.stitch.run as runner
        from python.stitch import profiles

        overhead = profiles.get("overhead")
        args = argparse.Namespace(asset_ppm=overhead.ppm,
                                  blend_px=overhead.blend_px,
                                  crop_bottom=overhead.crop_bottom,
                                  clip_uv=overhead.clip_uv)
        options, stamp = runner.asset_options(overhead, args)

        self.assertTrue(stamp.startswith("overhead "))
        self.assertIn("170.0", stamp)
        self.assertIn("85.0", stamp)
        self.assertIn("none", stamp)
        self.assertIn("--clip-uv", options)
        self.assertIn("3840", options)          # source size reaches the compiler

    def test_config_path_is_named_after_the_profile(self):
        from python.stitch import profiles

        self.assertEqual(profiles.get("overhead").config_path("metal").name,
                         "overhead_metal.conf")
        self.assertEqual(profiles.get("underwater").config_path("d3d11").name,
                         "underwater_d3d11.conf")
```

- [ ] **Step 3: 运行测试确认失败**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.RunProfileTest -v 2>&1 | tail -6
```
Expected: 多个用例 FAIL —— `write_config()` 还是旧签名
（`TypeError: write_config() takes from 4 to 5 positional arguments but 6 were given`），
且 `runner.stamp_path` / 新版 `asset_options` 尚不存在。

- [ ] **Step 4: 改 `run.py` 的模块头**

`python/stitch/run.py` 第 1-40 行（文档串 + import + 模块常量）整段替换为：

```python
"""One-command plane stitch: extract, compile, build, run.

Cross-platform by construction — every step is the same Python here, and the
platform only decides which CMake generator, backend name, and executable path
to use. macOS gets Metal, Windows gets D3D11 (or CUDA/GL with --backend cudagl).

Which model, how many lanes, what pixel density, whether the clips carry a wall
clock: all of that is the profile's, not this module's. Adding a stitch line
does not touch this file.

Each step is skipped when its output is already newer than its inputs, so the
common case (rerun after changing nothing) goes straight to the run.

    python -m python.stitch.run --profile overhead --video-dir DIR
    python -m python.stitch.run --video-dir DIR --seconds 30 --encode
    python -m python.stitch.run --steps asset,run          # skip extraction
"""
import argparse
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from python.stitch import profiles as P
from python.stitch import render_video as RV
from python.stitch.profiles import CONFIGS, PROJECT_ROOT, StepError

STEPS = ("extract", "asset", "build", "run")
```

（`StepError` 在 Task 2 已移入 `profiles`；这里重新导出，`runner.StepError` 的现有
引用与测试继续有效。原本定义在 `run.py:43-44` 的那个 class 删掉。）

- [ ] **Step 5: 改 `step_extract` 与 asset 相关三个函数**

`run.py` 的 `step_extract`（原第 97-106 行）、`asset_options`（原 109-117）、
`step_asset`（原 120-131）替换为：

```python
def stamp_path(profile):
    """Where the asset's shaping fingerprint lives, one file per profile."""
    return profile.asset.with_suffix(".stamp")


def step_extract(profile, args):
    if not profile.fbx.is_file():
        raise StepError(f"FBX does not exist: {profile.fbx}")
    if newer_than(profile.mesh_json, profile.fbx) and not args.force:
        print(f"mesh up to date: {profile.mesh_json}")
        return
    command = [python_bin(), "-m", "python.stitch.extract",
               profile.fbx, profile.mesh_json, "--tex-dir", profile.tex_dir]
    if profile.planes_only:
        command.append("--planes-only")
    run(command)


def asset_options(profile, args):
    """The asset-shaping arguments, plus a stamp string identifying them.

    The stamp leads with the profile name so two lines writing sibling .stamp
    files can never satisfy each other's up-to-date check."""
    options = ["--ppm", str(args.asset_ppm), "--no-neg-v",
               "--blend-px", str(args.blend_px),
               "--crop-bottom", str(args.crop_bottom),
               "--source-size", str(profile.source_size[0]),
               str(profile.source_size[1])]
    if args.clip_uv:
        options.append("--clip-uv")
    return options, " ".join([profile.name, *options])


def step_asset(profile, args):
    if not profile.mesh_json.is_file():
        raise StepError(
            f"mesh JSON missing (run the extract step): {profile.mesh_json}")
    options, stamp = asset_options(profile, args)
    stamp_file = stamp_path(profile)
    current = stamp_file.read_text() if stamp_file.is_file() else None
    if (newer_than(profile.asset, profile.mesh_json) and current == stamp
            and not args.force):
        print(f"asset up to date: {profile.asset}")
        return
    profile.asset.parent.mkdir(parents=True, exist_ok=True)
    run([python_bin(), "-m", "python.assets.compile_runtime_asset",
         profile.mesh_json, profile.asset,
         "--camera-ids", *profile.camera_ids, *options])
    stamp_file.write_text(stamp)
```

- [ ] **Step 6: 改 `step_build` 签名**

`step_build` 只用到 `args.backend`，但为了四个 step 签名一致（dispatcher 统一调用），
加上 `profile` 首参并标注不用：

```python
def step_build(profile, args):        # noqa: ARG001 - signature parity
    build_dir = build_dir_for(args.backend)
```

其余函数体不动。

- [ ] **Step 7: 改 `lane_start_offsets` 与 `write_config`**

`run.py` 的 `lane_start_offsets`（原 159-189）与 `write_config`（原 192-228）替换为：

```python
def lane_start_offsets(profile, video_dir):
    """Per-camera milliseconds into each clip where the common time axis starts.

    Recorded clips do not always share a t=0: each stream begins at its own
    decodable keyframe, placed inside the lookback window with GOP granularity,
    so the per-lane skew reaches seconds. Profiles whose samples carry that
    wall-clock truth (sync="manifest") reuse render_video's reading of it, so the
    realtime path aligns by exactly the same formula as the offline renderer.

    Profiles with sync="none" return {} without looking: their recordings have
    no manifest by design, and reporting that as an exception every run would be
    noise. A manifest-bearing profile whose manifest is missing degrades to the
    same empty result, but says so."""
    if profile.sync != "manifest":
        return {}
    try:
        align_start, align_end, fps, cams = RV.load_manifest(video_dir)
    except SystemExit as error:
        print(f"  no wall-clock alignment: {error}")
        return {}
    order = [camera for camera in profile.camera_ids if camera in cams]
    starts, report = RV.alignment_plan(align_start, align_end, fps, cams, order)
    offsets = {}
    for camera, entry in zip(order, report):
        # A negative skew means the clip begins after align_start; that lane has
        # no coverage at t=0 and its offset clamps to zero.
        offsets[camera] = max(0, entry["skew_ms"])
    skews = [entry["skew_ms"] for entry in report]
    print(f"  wall-clock align window {(align_end - align_start) / 1000:.3f}s; "
          f"lane skew {min(skews)}..{max(skews)}ms")
    for entry in report:
        if entry["late_start"]:
            print(f"  QC {entry['cam']}: starts {-entry['skew_ms']}ms after "
                  "align_start (no coverage at t=0)")
    return offsets


def write_config(profile, path, video_dir, backend, encode_path, align=True):
    """Emit a runtime config naming the profile's lanes left-to-right.

    Written fresh each run so the clip directory and backend always match what
    was asked for; the C++ loader takes camera identity straight from these
    `source.<id>` lines."""
    clips = {camera: profile.clip_for(video_dir, camera)
             for camera in profile.camera_ids}
    offsets = lane_start_offsets(profile, video_dir) if align else {}

    lines = [f"backend={backend}", "mode=realtime", "stage=full",
             f"asset={profile.asset.as_posix()}"]
    for camera in profile.camera_ids:
        lines.append(f"source.{camera}={clips[camera].as_posix()}")
        if offsets.get(camera):
            lines.append(f"source.{camera}.start_ms={offsets[camera]}")
    lines += ["fps_num=30000", "fps_den=1001",
              "preview=true", "encode=false", "diagnostic_replacement=false",
              f"encode_path={Path(encode_path).as_posix()}",
              "stale_ms=100", "replace_ms=1000",
              "decode_surface_pool=8", "decode_ticket_pool=16",
              "render_inflight=3", "output_pool=4",
              "duration_seconds=10",
              f"metrics={profile.metrics.as_posix()}"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    aligned = sum(1 for camera in profile.camera_ids if offsets.get(camera))
    print(f"wrote config {path} ({len(profile.camera_ids)} lanes, "
          f"{aligned} with a start offset)")
```

- [ ] **Step 8: 改 `step_run`**

`run.py` 的 `step_run`（原 231-263）前 10 行替换为：

```python
def step_run(profile, args):
    build_dir = build_dir_for(args.backend)
    executable = executable_for(build_dir)
    if not executable.is_file():
        raise StepError(f"executable missing (run the build step): {executable}")
    if not profile.asset.is_file():
        raise StepError(f"asset missing (run the asset step): {profile.asset}")

    encode_path = Path(args.encode_path)
    config = Path(args.config) if args.config else profile.config_path(args.backend)
    if args.config is None:
        write_config(profile, config, args.video_dir, args.backend, encode_path,
                     align=args.align)
    elif not config.is_file():
        raise StepError(f"config does not exist: {config}")
```

函数余下部分（构造 `command`、`run(command)`、打印）不动。

- [ ] **Step 9: 改 `parse_args` 与 `main`**

`parse_args`（原 266-319）里删掉 `--fbx` / `--tex-dir`（它们现在是 profile 的），
加 `--profile`，并把三个 profile 相关默认值改为 `None` 后回落：

```python
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="One-command plane stitch (macOS + Windows)")
    parser.add_argument("--profile", default="underwater",
                        choices=P.names(),
                        help="stitch line to run (default: %(default)s)")
    parser.add_argument("--video-dir", type=Path,
                        help="directory holding one clip per camera")
    parser.add_argument("--backend", default=default_backend(),
                        choices=("metal", "d3d11", "cudagl"))
    parser.add_argument("--steps", default=",".join(STEPS),
                        help=f"comma-separated subset of {','.join(STEPS)}")
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--fps", type=int, default=None,
                        help="override the render cadence (default: clip fps)")
    parser.add_argument("--no-preview", dest="preview", action="store_false",
                        default=True)
    parser.add_argument("--no-window", dest="window", action="store_false",
                        default=True, help="render offscreen (no preview window)")
    parser.add_argument("--encode", action="store_true",
                        help="also write HEVC to --encode-path")
    parser.add_argument("--encode-path", type=Path, default=None,
                        help="HEVC destination (default: "
                             "outputs/videos/<profile>_realtime.h265)")
    parser.add_argument("--metrics", type=Path, default=None,
                        help="metrics JSONL (default: the profile's)")
    parser.add_argument("--config", type=Path, default=None,
                        help="use this runtime config instead of generating one")
    parser.add_argument("--force", action="store_true",
                        help="redo steps even when their outputs look current")

    shaping = parser.add_argument_group(
        "composite shaping",
        "These control how the .swasset is baked, so the realtime stitch "
        "matches what python.stitch.render_video produces offline. Each "
        "defaults to the profile's value; changing any recompiles the asset.")
    shaping.add_argument("--asset-ppm", type=float, default=None,
                         help="output pixels per metre")
    shaping.add_argument("--blend-px", type=float, default=None,
                         help="vertical seam transition width in pixels; "
                              "0 is a hard cut")
    shaping.add_argument("--no-clip-uv", dest="clip_uv", action="store_false",
                         default=True,
                         help="keep pixels whose UV falls outside the source "
                              "image (the GPU mirror-samples them); clipping "
                              "is on by default to match the offline renderer")
    shaping.add_argument("--crop-bottom", default=None,
                         metavar="auto|none|N",
                         help="drop bottom rows the shorter planes leave "
                              "uncovered")
    shaping.add_argument("--no-align", dest="align", action="store_false",
                         default=True,
                         help="ignore the manifest wall clocks and read every "
                              "clip from its first frame")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    profile = P.get(args.profile)
    # Unset shaping options fall back to the profile, so `--profile overhead`
    # alone bakes exactly what the design specifies.
    if args.asset_ppm is None:
        args.asset_ppm = profile.ppm
    if args.blend_px is None:
        args.blend_px = profile.blend_px
    if args.crop_bottom is None:
        args.crop_bottom = profile.crop_bottom
    if args.metrics is None:
        args.metrics = profile.metrics
    if args.encode_path is None:
        args.encode_path = (PROJECT_ROOT / "outputs" / "videos" /
                            f"{profile.name}_realtime.h265")

    steps = [step.strip() for step in args.steps.split(",") if step.strip()]
    unknown = [step for step in steps if step not in STEPS]
    if unknown:
        raise SystemExit(f"unknown steps: {', '.join(unknown)}; "
                         f"valid: {', '.join(STEPS)}")
    if "run" in steps and args.config is None and args.video_dir is None:
        raise SystemExit("--video-dir is required (or pass --config)")

    handlers = {"extract": step_extract, "asset": step_asset,
                "build": step_build, "run": step_run}
    try:
        for step in steps:
            print(f"\n=== {step} ===", flush=True)
            handlers[step](profile, args)
    except StepError as error:
        raise SystemExit(f"error: {error}")
    print("\ndone.")
```

- [ ] **Step 10: 运行新旧测试确认通过**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.RunProfileTest \
  tests.python.test_stitch.OneClickRunnerTest \
  tests.python.test_stitch.LaneAlignmentConfigTest -v 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 11 tests in ...s
OK
```
（6 个新增 + `OneClickRunnerTest` 3 个 + `LaneAlignmentConfigTest` 2 个。）

- [ ] **Step 11: 跑全量测试**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 206 tests in ...s
OK
```
200（Task 4 后）+ 6 = 206。

- [ ] **Step 12: 冒烟测试 —— 两条线路的 config 都对**

```bash
.venv/bin/python -c "
import tempfile
from pathlib import Path
from python.stitch import profiles, run as runner

for name, session, cams in (
    ('overhead', '20260629_172532', ('cam5', 'cam6')),
    ('underwater', 'swb_test', tuple(f'underA{i}' for i in range(16, 0, -1))),
):
    profile = profiles.get(name)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for camera in cams:
            (td / f'{session}_{camera}{profile.clip_suffix}').write_bytes(b'')
        config = td / 'c.conf'
        runner.write_config(profile, config, td, 'metal', td / 'o.h265', align=False)
        lines = config.read_text().splitlines()
        sources = [l.split('=', 1)[0].removeprefix('source.')
                   for l in lines if l.startswith('source.')]
        asset = [l for l in lines if l.startswith('asset=')][0]
        print(f'{name:11s} lanes={len(sources):2d} first={sources[0]:9s} '
              f'last={sources[-1]:9s} asset={Path(asset.split(chr(61),1)[1]).name}')
        print(f'{\"\":11s} config={profile.config_path(\"metal\").name} '
              f'stamp={runner.stamp_path(profile).name}')
"
```
Expected:
```
overhead    lanes= 2 first=cam5      last=cam6      asset=overhead.swasset
            config=overhead_metal.conf stamp=overhead.stamp
underwater  lanes=16 first=underA16  last=underA1   asset=underwater.swasset
            config=underwater_metal.conf stamp=underwater.stamp
```

- [ ] **Step 13: 提交**

```bash
git add python/stitch/run.py tests/python/test_stitch.py
git commit -F - <<'EOF'
refactor(stitch): drive the runner from the profile

Seven module constants and a hardcoded *_{camera}.ts glob decided which model
got extracted, how many lanes the config declared, what density the asset baked
at, and where everything landed. A second line could not reuse any of it.

They are now profile lookups, which also fixes two things that were latent
rather than hypothetical:

- the .stamp file sat beside the asset under a name derived from it, so two
  lines would have written sibling stamps and could satisfy each other's
  up-to-date check. The stamp now leads with the profile name.
- lane_start_offsets caught SystemExit from load_manifest and printed "no
  wall-clock alignment" — correct for an underwater sample that should have had
  a manifest, wrong for an overhead recording that never has one. It now skips
  the read entirely when the profile says the clips carry no wall clock, and
  still reports the degradation when a manifest-bearing line is missing its own.

--fbx and --tex-dir are gone from the CLI: they were the profile's identity
smuggled in as overrides, and pointing them at another model while keeping the
profile's camera ids would have produced a mismatch caught much later.
EOF
```

---

## Task 6: 一张步骤表取代手写的 uw-* 笛卡尔积

**Files:**
- Create: `python/stitch/__main__.py`
- Create: `scripts/run_stitch.sh`
- Create: `scripts/run_stitch.ps1`
- Modify: `scripts/run_python.sh`（删 `uw-*` 五个函数、三个 `UW_*` 变量、usage 与
  dispatch 的对应行）
- Delete: `scripts/run_underwater.sh`、`scripts/run_underwater.ps1`
- Test: `tests/python/test_stitch.py`（追加 `DispatcherTest`）

**Interfaces:**
- Consumes: Task 2 `profiles`；Task 3 `render_video.render_video`；Task 4
  `export_ref_tex.export` / `tex_names`；Task 5 `run.step_extract` /
  `step_asset` / `step_build` / `step_run`；`render.render_stills`。
- Produces:
  - `python/stitch/__main__.py` 的 `STEPS: dict[str, callable]` —— 键序即帮助里的
    展示序：`extract`、`tex`、`still`、`video`、`asset`、`build`、`live`。
  - `parse_args(argv) -> argparse.Namespace` —— 位置参数 `profile`、`steps`；
    其余为可选覆盖。
  - `main(argv=None) -> None` —— 未知 profile 或未知步骤均抛 `SystemExit` 且消息列出
    合法值。
  - `step_still(profile, args)`、`step_tex(profile, args)`、`step_video(profile, args)`
    —— 与 Task 5 四个 step 同签名 `(profile, args)`。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/python/test_stitch.py` 末尾：

```python
class DispatcherTest(unittest.TestCase):
    """One step table, profile as an argument — so a third line costs a profile
    record and no new CLI surface."""

    def test_step_table_lists_offline_then_realtime(self):
        from python.stitch import __main__ as cli

        self.assertEqual(list(cli.STEPS),
                         ["extract", "tex", "still", "video", "asset",
                          "build", "live"])

    def test_every_step_takes_profile_and_args(self):
        import inspect
        from python.stitch import __main__ as cli

        for name, handler in cli.STEPS.items():
            parameters = list(inspect.signature(handler).parameters)
            self.assertEqual(parameters, ["profile", "args"],
                             f"step {name} has signature {parameters}")

    def test_unknown_step_lists_the_valid_ones(self):
        from python.stitch import __main__ as cli

        with self.assertRaises(SystemExit) as caught:
            cli.main(["overhead", "polish"])
        message = str(caught.exception)
        self.assertIn("polish", message)
        self.assertIn("extract", message)
        self.assertIn("live", message)

    def test_unknown_profile_is_rejected_before_any_step_runs(self):
        from python.stitch import __main__ as cli

        with self.assertRaises(SystemExit) as caught:
            cli.main(["pool", "extract"])
        message = str(caught.exception)
        self.assertIn("pool", message)
        self.assertIn("underwater", message)

    def test_steps_run_in_the_order_given(self):
        from python.stitch import __main__ as cli

        order = []
        original = dict(cli.STEPS)
        try:
            for name in ("extract", "asset"):
                cli.STEPS[name] = (
                    lambda profile, args, _name=name: order.append(_name))
            cli.main(["overhead", "asset,extract"])
        finally:
            cli.STEPS.clear()
            cli.STEPS.update(original)
        self.assertEqual(order, ["asset", "extract"])

    def test_video_and_live_require_a_video_dir(self):
        from python.stitch import __main__ as cli

        for step in ("video", "live"):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["overhead", step])
            self.assertIn("--video-dir", str(caught.exception))

    def test_tex_requires_a_video_dir_only_when_the_source_is_video(self):
        from python.stitch import __main__ as cli

        # overhead reads reference textures from clips, so tex needs the dir
        with self.assertRaises(SystemExit) as caught:
            cli.main(["overhead", "tex"])
        self.assertIn("--video-dir", str(caught.exception))
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.DispatcherTest -v 2>&1 | tail -5
```
Expected: `ModuleNotFoundError: No module named 'python.stitch.__main__'`

- [ ] **Step 3: 写 `python/stitch/__main__.py`**

```python
"""Run one stitch line's steps: python -m python.stitch <profile> <steps>.

    python -m python.stitch overhead extract,still
    python -m python.stitch underwater still --real --blend-px 120
    python -m python.stitch overhead extract,asset,build,live --video-dir DIR

Steps are a table, the line is an argument. The alternative — a subcommand per
(line, step) pair — is what this replaces: five uw-* shell functions that
differed only in which paths they filled in, so a third line meant copying all
five again.

Skipping is per step kind, not uniform. extract/tex/asset are intermediates and
skip when their output is newer than their inputs (asset also compares a stamp of
its shaping options); --force redoes them. still/video always render: their
shaping is overridable from the command line and mtime cannot see that, so a
stale image that looks fresh would be a trap. build keeps its own check (the
executable exists).
"""
import argparse
from pathlib import Path

from python.stitch import export_ref_tex, profiles as P, render as R
from python.stitch import render_video as RV
from python.stitch import run as realtime
from python.stitch.profiles import StepError


def step_tex(profile, args):
    """Export one reference texture per camera (the frame the stitch sees)."""
    out_dir = profile.ref_tex_dir
    if (out_dir.is_dir() and any(out_dir.iterdir()) and not args.force):
        print(f"reference textures present: {out_dir}")
        return
    export_ref_tex.export(profile, out_dir=out_dir, video_dir=args.video_dir)


def step_still(profile, args):
    """Composite, grid diagnostic and fusion heatmap for one line.

    --real swaps the designer's calibration frames for the exported camera
    frames; the outputs take a _real suffix so the two never overwrite."""
    if not profile.mesh_json.is_file():
        raise StepError(
            f"mesh JSON missing (run the extract step): {profile.mesh_json}")
    if args.real:
        tex_dir = profile.ref_tex_dir
        if not tex_dir.is_dir():
            raise StepError(
                f"reference textures missing (run the tex step): {tex_dir}")
        tex_names = export_ref_tex.tex_names(profile)
        suffix = "_real"
    else:
        tex_dir = profile.still_tex_dir
        if not tex_dir.is_dir():
            raise StepError(
                f"still texture directory missing: {tex_dir} "
                "(set STITCH_GRID_DIR or ANNOTATION_PREVIEW_DATASET_ROOT)")
        tex_names = None
        suffix = ""
    out = profile.out_dir
    R.render_stills(
        profile.mesh_json, tex_dir,
        out / f"stitch{suffix}.png", out / f"grid{suffix}.png",
        ppm=args.ppm if args.ppm is not None else profile.ppm,
        neg_v=False,
        blend_px=args.blend_px if args.blend_px is not None else profile.blend_px,
        full_res=profile.full_res and not args.no_full_res,
        heatmap_path=out / f"heat{suffix}.png",
        tex_names=tex_names,
    )


def step_video(profile, args):
    """Stitch every camera's clip into one panorama mp4."""
    if not profile.mesh_json.is_file():
        raise StepError(
            f"mesh JSON missing (run the extract step): {profile.mesh_json}")
    RV.render_video(
        profile.mesh_json, args.video_dir, profile.out_dir / "stitch.mp4",
        camera_ids=profile.camera_ids,
        clip_for=profile.clip_for,
        seconds=args.seconds_float,
        ppm=args.ppm if args.ppm is not None else profile.ppm,
        neg_v=False,
        blend_px=args.blend_px if args.blend_px is not None else profile.blend_px,
        full_res=profile.full_res and not args.no_full_res,
        align=profile.sync == "manifest" and not args.no_align,
    )


# Offline first, then the realtime chain — the order a new line gets brought up.
STEPS = {
    "extract": realtime.step_extract,
    "tex": step_tex,
    "still": step_still,
    "video": step_video,
    "asset": realtime.step_asset,
    "build": realtime.step_build,
    "live": realtime.step_run,
}

# Steps that cannot work without clips to read.
_NEEDS_VIDEO = ("video", "live")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m python.stitch",
        description="Run a plane-stitch line's steps",
        epilog=f"steps: {', '.join(STEPS)}")
    parser.add_argument("profile", help=f"one of: {', '.join(P.names())}")
    parser.add_argument("steps", help="comma-separated steps, run in order")
    parser.add_argument("--video-dir", type=Path, default=None,
                        help="clip directory, one clip per camera")
    parser.add_argument("--real", action="store_true",
                        help="still: use the exported camera frames instead of "
                             "the designer's calibration textures")
    parser.add_argument("--ppm", type=float, default=None,
                        help="override the profile's pixels per metre")
    parser.add_argument("--blend-px", type=float, default=None,
                        help="override the profile's seam transition width")
    parser.add_argument("--no-full-res", action="store_true",
                        help="skip the rescale back to source height")
    parser.add_argument("--no-align", action="store_true",
                        help="read every clip from frame 0")
    parser.add_argument("--seconds", type=int, default=30,
                        help="live: run duration (default: %(default)s)")
    parser.add_argument("--seconds-float", type=float, default=None,
                        help="video: cap output duration; default is the whole "
                             "align window")
    parser.add_argument("--backend", default=realtime.default_backend(),
                        choices=("metal", "d3d11", "cudagl"))
    parser.add_argument("--fps", type=int, default=None,
                        help="live: override the render cadence")
    parser.add_argument("--no-preview", dest="preview", action="store_false",
                        default=True)
    parser.add_argument("--no-window", dest="window", action="store_false",
                        default=True, help="live: render offscreen")
    parser.add_argument("--encode", action="store_true",
                        help="live: also write HEVC")
    parser.add_argument("--encode-path", type=Path, default=None)
    parser.add_argument("--metrics", type=Path, default=None)
    parser.add_argument("--config", type=Path, default=None,
                        help="live: use this runtime config instead of "
                             "generating one")
    parser.add_argument("--no-clip-uv", dest="clip_uv", action="store_false",
                        default=True,
                        help="asset: keep pixels whose UV falls outside the "
                             "source image")
    parser.add_argument("--crop-bottom", default=None, metavar="auto|none|N",
                        help="asset: override the profile's bottom crop")
    parser.add_argument("--force", action="store_true",
                        help="redo steps whose outputs look current")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    profile = P.get(args.profile)

    steps = [step.strip() for step in args.steps.split(",") if step.strip()]
    unknown = [step for step in steps if step not in STEPS]
    if unknown:
        raise SystemExit(f"unknown steps: {', '.join(unknown)}; "
                         f"valid: {', '.join(STEPS)}")
    if not steps:
        raise SystemExit(f"no steps given; valid: {', '.join(STEPS)}")

    needs_video = [step for step in steps if step in _NEEDS_VIDEO]
    if profile.ref_tex == "video" and "tex" in steps:
        needs_video.append("tex")
    if needs_video and args.video_dir is None and args.config is None:
        raise SystemExit(
            f"--video-dir is required for: {', '.join(sorted(set(needs_video)))}")

    # The realtime steps read the shaping values off `args`; fill the profile's
    # in so `live` behaves the same whether it was reached from here or from
    # python.stitch.run.
    args.asset_ppm = args.ppm if args.ppm is not None else profile.ppm
    if args.blend_px is None:
        args.blend_px = profile.blend_px
    if args.crop_bottom is None:
        args.crop_bottom = profile.crop_bottom
    if args.metrics is None:
        args.metrics = profile.metrics
    if args.encode_path is None:
        args.encode_path = (P.PROJECT_ROOT / "outputs" / "videos" /
                            f"{profile.name}_realtime.h265")
    args.align = not args.no_align

    profile.out_dir.mkdir(parents=True, exist_ok=True)
    try:
        for step in steps:
            print(f"\n=== {profile.name}: {step} ===", flush=True)
            STEPS[step](profile, args)
    except StepError as error:
        raise SystemExit(f"error: {error}")
    print("\ndone.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: 运行新测试确认通过**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.DispatcherTest -v 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 7 tests in ...s
OK
```

- [ ] **Step 5: 写 `scripts/run_stitch.sh`**

```bash
#!/usr/bin/env bash
# 平面拼接统一入口（macOS / Linux）。
#
# 用法:
#   ./scripts/run_stitch.sh PROFILE STEPS [选项…]
#
# 例:
#   ./scripts/run_stitch.sh overhead extract,still
#   ./scripts/run_stitch.sh underwater still --real --blend-px 120
#   ./scripts/run_stitch.sh overhead extract,asset,build,live --video-dir DIR
#
# 全部逻辑在 python/stitch/__main__.py（mac 与 Windows 共用同一份），这个脚本只
# 负责挑选解释器并原样转发。Windows 用 scripts/run_stitch.ps1。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

if (($# < 2)); then
  cat >&2 <<'EOF'
Usage: scripts/run_stitch.sh PROFILE STEPS [options]

PROFILE  underwater | overhead
STEPS    逗号分隔，按给出顺序执行:
           extract  FBX -> mesh JSON
           tex      导出每台相机的参考贴图（首帧）
           still    静图 + 网格诊断图 + 融合热图
           video    每路片段 -> 全景 mp4
           asset    mesh JSON -> GPU .swasset
           build    构建 swim_realtime
           live     实时拼接（预览 / HEVC / 指标）

Common options (full list: --help):
  --video-dir DIR    片段目录（video / live，以及从视频取贴图的 tex 必需）
  --real             still 用导出的相机帧，而非设计师标定图
  --seconds N        live 运行秒数（默认 30）
  --encode           live 同时写出 HEVC
  --no-window        live 离屏渲染
  --blend-px N       覆盖 profile 的接缝过渡宽度
  --ppm N            覆盖 profile 的每米像素数
  --force            即使产物是新的也重做
EOF
  exit 2
fi

cd "$ROOT"
exec "$PY" -m python.stitch "$@"
```

改可执行位：

```bash
chmod +x scripts/run_stitch.sh
```

- [ ] **Step 6: 写 `scripts/run_stitch.ps1`**

```powershell
# 平面拼接统一入口（Windows）。
#
# 用法:
#   pwsh scripts/run_stitch.ps1 PROFILE STEPS [选项…]
#   pwsh scripts/run_stitch.ps1 overhead extract,still
#   pwsh scripts/run_stitch.ps1 underwater extract,asset,build,live -- --video-dir D:\SWIM\swb_x
#
# 全部逻辑在 python/stitch/__main__.py（与 macOS 共用同一份）。这个脚本只挑选
# 解释器并原样转发 —— 参数不在这里重新声明，否则每加一个 CLI 选项都要改两处。
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot

function Resolve-Python {
  $venv = Join-Path $Root '.venv/Scripts/python.exe'
  if (Test-Path $venv) { return $venv }
  foreach ($name in @('python', 'python3', 'py')) {
    $found = Get-Command $name -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
  }
  throw 'no Python interpreter found (create .venv with numpy+opencv first)'
}

$python = Resolve-Python
Push-Location $Root
try {
  & $python -m python.stitch @args
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
  Pop-Location
}
```

- [ ] **Step 7: 从 `run_python.sh` 删掉 `uw-*`**

删除 `scripts/run_python.sh` 的这些片段：

1. usage 注释里的五行（原第 13-17 行）：
```
#   ./scripts/run_python.sh uw-extract          # all.fbx -> 16-plane mesh JSON
#   ./scripts/run_python.sh uw-tex               # export per-camera first frames
#   ./scripts/run_python.sh uw-render [BLEND_PX] # stitch (grid textures)
#   ./scripts/run_python.sh uw-real [BLEND_PX]   # stitch (real first-frame images)
#   ./scripts/run_python.sh uw-video DIR [BP] [S] # stitch 16 clips -> mp4
```

2. `usage()` 里 Commands 段的五行（原第 44-48 行 `uw-extract` … `uw-video`）
   与 Examples 段的三行（原第 62-64 行 `uw-extract` / `uw-tex` / `uw-real 120`）。

3. 整段 `# --- underwater N-plane stitch ...` 到 `cmd_uw_video()` 结束
   （原第 156-215 行）：三个变量 `UW_MODELS`/`UW_OUT`/`UW_DATASET`/`UW_GRID_DIR`
   与五个函数。

4. dispatch 里的五行（原第 254-258 行）：
```
  uw-extract) cmd_uw_extract "$@" ;;
  uw-tex) cmd_uw_tex "$@" ;;
  uw-render) cmd_uw_render "$@" ;;
  uw-real) cmd_uw_real "$@" ;;
  uw-video) cmd_uw_video "$@" ;;
```

在 usage 的 Commands 段末尾加一行指路：

```
  (拼接线路 underwater/overhead 已移到 scripts/run_stitch.sh)
```

- [ ] **Step 8: 删掉两个旧包装脚本**

```bash
git rm scripts/run_underwater.sh scripts/run_underwater.ps1
```

- [ ] **Step 9: 确认没有残留引用**

Run:
```bash
grep -rn "uw-extract\|uw-tex\|uw-render\|uw-real\|uw-video\|run_underwater" \
  scripts/ python/ tests/ CMakeLists.txt 2>/dev/null || echo "no stale references"
```
Expected: `no stale references`

（README 里仍有引用，Task 9 处理。）

- [ ] **Step 10: 确认 `run_python.sh` 仍能用**

Run:
```bash
bash -n scripts/run_python.sh && echo "syntax ok"
bash scripts/run_python.sh --help | head -20
```
Expected: `syntax ok`，随后 usage 输出里**没有** `uw-` 开头的命令，但仍有
`still` / `4k` / `keypoint` / `we-predict` 等。

- [ ] **Step 11: 确认新入口的帮助可读**

Run:
```bash
bash -n scripts/run_stitch.sh && echo "syntax ok"
bash scripts/run_stitch.sh 2>&1 | head -14
.venv/bin/python -m python.stitch --help 2>&1 | head -12
```
Expected: `syntax ok`；随后 shell usage 列出七个步骤；argparse 帮助的
`positional arguments` 段出现 `profile` 与 `steps`，epilog 出现
`steps: extract, tex, still, video, asset, build, live`。

- [ ] **Step 12: 跑全量测试**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 213 tests in ...s
OK
```
206（Task 5 后）+ 7 = 213。

- [ ] **Step 13: 冒烟测试 —— 水下静图经新入口仍逐像素一致**

```bash
STITCH_GRID_DIR=outputs/underwater/real_tex_all \
  .venv/bin/python -m python.stitch underwater still --blend-px 120
.venv/bin/python -c "
import cv2, numpy as np
a = cv2.imread('outputs/underwater/all_real_stitch_bp120.png')
b = cv2.imread('outputs/underwater/stitch.png')
assert a is not None and b is not None
assert a.shape == b.shape, f'{a.shape} != {b.shape}'
assert np.array_equal(a, b), f'max diff {cv2.absdiff(a,b).max()}'
print('identical', a.shape)
"
```
Expected: `identical (360, 3278, 3)`

这里用 `STITCH_GRID_DIR` 指向 `real_tex_all`（旧的 basename 命名目录）来复现旧产物 ——
它证明 dispatcher 传下去的 `ppm`/`blend_px`/`full_res` 与旧 shell 完全一致。

- [ ] **Step 14: 提交**

```bash
git add -A scripts/ python/stitch/__main__.py tests/python/test_stitch.py
git commit -F - <<'EOF'
feat(stitch): one step table, profile as an argument

The shell had a subcommand per (line, step) pair: five uw-* functions that
differed only in which paths they filled in, plus two run_underwater wrappers.
Adding a line meant copying all five, and run_underwater.ps1 had additionally
transcribed run.py's argparse into a 12-entry param() block, so every new option
had to be added twice.

Steps are now a dict and the line is a positional argument, which also lets the
offline and realtime steps share one skip policy discussion: intermediates
(extract/tex/asset) check mtime, renders (still/video) always run because their
shaping is overridable and mtime cannot see that.

run_python.sh keeps the pool, keypoint, annotation and water-entry commands —
those are not stitch lines.
EOF
```

---

## Task 7: FBX 落位与 overhead 离线全链路

**Files:**
- Create: `inputs/overhead/models/002.fbx`（从 `inputs/002.fbx` 移入）
- Create: `inputs/overhead/models/002.fbm/05-02.jpg`、`C06.jpg`（从 `inputs/002.fbm/` 移入）
- Modify: `.gitignore`（加 `inputs/overhead/models/`；config glob 泛化；注释更名）
- Test: `tests/python/test_stitch.py`（追加 `OverheadExtractTest`）

**Interfaces:**
- Consumes: Task 2 `profiles.get("overhead")`、Task 4 `export_ref_tex`、Task 6 dispatcher。
- Produces: `outputs/overhead/mesh.json`（2 块，顺序 `Plane002`→`Plane001`）、
  `stitch.png` / `grid.png` / `heat.png`（4255×515）、`ref_tex/cam5.png`、`ref_tex/cam6.png`、
  `stitch.mp4`。后续任务不依赖这些产物的具体像素，只依赖 `mesh.json` 存在。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/python/test_stitch.py` 末尾：

```python
MODEL_002 = PROJECT_ROOT / "inputs" / "overhead" / "models" / "002.fbx"
TEXDIR_002 = PROJECT_ROOT / "inputs" / "overhead" / "models" / "002.fbm"


class OverheadExtractTest(unittest.TestCase):
    @unittest.skipUnless(HAS_FBX, "FBX SDK not available")
    @unittest.skipUnless(MODEL_002.is_file(), "002.fbx not present")
    def test_extracts_two_planes_left_to_right(self):
        import tempfile
        from python.stitch.extract import extract_to_json

        with tempfile.TemporaryDirectory() as td:
            dst = Path(td) / "mesh.json"
            meshes = extract_to_json(MODEL_002, dst, TEXDIR_002)

            self.assertEqual(len(meshes), 2)
            # world X ascending: Plane002 starts at -35.22, Plane001 at -27.72
            self.assertEqual([m["node"] for m in meshes],
                             ["Plane002", "Plane001"])
            # which pins cam5 -> 05-02.jpg and cam6 -> C06.jpg positionally
            self.assertEqual([m["texture_basename"] for m in meshes],
                             ["05-02.jpg", "C06.jpg"])

    @unittest.skipUnless(HAS_FBX, "FBX SDK not available")
    @unittest.skipUnless(MODEL_002.is_file(), "002.fbx not present")
    def test_spans_one_lane_the_same_size_as_the_underwater_panorama(self):
        # Both models cover the same 25.000m x 3.000m lane; that is why one set
        # of geometry code serves both.
        import tempfile
        from python.stitch.extract import extract_to_json

        with tempfile.TemporaryDirectory() as td:
            meshes = extract_to_json(MODEL_002, Path(td) / "m.json", TEXDIR_002)

        xs = [v["pos"][0] for m in meshes for t in m["triangles"] for v in t]
        ys = [v["pos"][1] for m in meshes for t in m["triangles"] for v in t]
        self.assertAlmostEqual(max(xs) - min(xs), 25.0, places=3)
        self.assertAlmostEqual(max(ys) - min(ys), 3.0, places=3)

    @unittest.skipUnless(HAS_FBX, "FBX SDK not available")
    @unittest.skipUnless(MODEL_002.is_file(), "002.fbx not present")
    def test_planes_only_would_reject_this_model(self):
        # The profile sets planes_only=False, and that is not merely tidiness:
        # select_pool_planes keeps meshes whose world-Y falls inside the pool
        # band (-11.6, -8.0), which is where the underwater planes sit. 002.fbx
        # is an overhead model spanning Y [20.47, 23.47], so the filter would
        # drop both planes and extraction would exit with "no pool plane found".
        import tempfile
        from python.stitch.extract import extract_to_json, select_pool_planes

        with tempfile.TemporaryDirectory() as td:
            meshes = extract_to_json(MODEL_002, Path(td) / "m.json", TEXDIR_002)
        self.assertEqual(len(meshes), 2)
        self.assertEqual(select_pool_planes(meshes), [])
```

- [ ] **Step 2: 运行测试确认跳过（模型还没落位）**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.OverheadExtractTest -v 2>&1 | grep -E "skipped|^(Ran|OK)"
```
Expected: 三个用例都 `skipped '002.fbx not present'` —— 确认 skip 条件本身有效，
落位后才会真正运行。

- [ ] **Step 3: 移动 FBX 与贴图**

```bash
mkdir -p inputs/overhead/models
mv inputs/002.fbx inputs/overhead/models/002.fbx
mv inputs/002.fbm inputs/overhead/models/002.fbm
ls -la inputs/overhead/models/ inputs/overhead/models/002.fbm/
```
Expected: `002.fbx`（约 6.8 MB）、`002.fbm/` 下 `05-02.jpg` 与 `C06.jpg`。

- [ ] **Step 4: 改 `.gitignore`**

`.gitignore` 现有的 `inputs/*.fbx` 与 `inputs/*.fbm/`（第 27-28 行）**不再匹配**移入
子目录后的文件（`*` 不跨层级），那个 6.8 MB 的重资产会变成可提交状态。按仓库既有惯例
补一条整目录忽略。

把 `.gitignore` 的这一段：

```
# Controlled inputs that are local-only (heavy FBX assets, not the committed
# pool fixture already tracked at inputs/pool/models/pool.fbx).
inputs/underwater/models/
inputs/pool/models/*.fbx
```

改为：

```
# Controlled inputs that are local-only (heavy FBX assets, not the committed
# pool fixture already tracked at inputs/pool/models/pool.fbx).
inputs/underwater/models/
inputs/overhead/models/
inputs/pool/models/*.fbx
```

把末尾这一段：

```
# Runtime configs generated per machine by python.underwater.run
inputs/configs/underwater_16_*.conf
inputs/*.fbx
inputs/*.fbm/
```

改为：

```
# Runtime configs generated per machine by python.stitch
inputs/configs/underwater_*.conf
inputs/configs/overhead_*.conf
# Loose model drops straight into inputs/ (before they are filed under a line)
inputs/*.fbx
inputs/*.fbm/
```

- [ ] **Step 5: 确认忽略生效、无重资产入库**

Run:
```bash
git check-ignore -v inputs/overhead/models/002.fbx
git check-ignore -v inputs/overhead/models/002.fbm/C06.jpg
git status --short | grep -E "002|overhead" || echo "no overhead assets staged"
```
Expected: 前两条各输出一行 `.gitignore:NN:inputs/overhead/models/  ...`；
第三条输出 `no overhead assets staged`。

- [ ] **Step 6: 运行测试确认真正跑起来并通过**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.OverheadExtractTest -v 2>&1 | grep -E "^(Ran|OK|FAILED|test_)"
```
Expected: 三个用例都 `ok`（不再是 skipped）：
```
Ran 3 tests in ...s
OK
```

- [ ] **Step 7: 跑全量测试**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 216 tests in ...s
OK
```
213（Task 6 后）+ 3 = 216。

- [ ] **Step 8: 端到端 —— 提取网格**

Run:
```bash
.venv/bin/python -m python.stitch overhead extract
```
Expected:
```
=== overhead: extract ===
$ ... -m python.stitch.extract .../002.fbx .../outputs/overhead/mesh.json --tex-dir .../002.fbm
Plane002     tris= 120 tex=05-02.jpg uvset=UVChannel_1
Plane001     tris= 204 tex=C06.jpg uvset=UVChannel_1
wrote .../outputs/overhead/mesh.json

done.
```
注意**没有** `--planes-only`（profile 里是 `False`）。

- [ ] **Step 9: 端到端 —— 标定图静图**

Run:
```bash
.venv/bin/python -m python.stitch overhead still
```
Expected:
```
=== overhead: still ===
canvas 4255x515 @ 170.00px/m
wrote still .../outputs/overhead/stitch.png
wrote grid still .../outputs/overhead/grid.png
wrote fusion heatmap .../outputs/overhead/heat.png

done.
```

- [ ] **Step 10: 目视检查静图与热图**

Run:
```bash
.venv/bin/python -c "
import cv2
for name in ('stitch', 'grid', 'heat'):
    image = cv2.imread(f'outputs/overhead/{name}.png')
    print(f'{name:7s} {image.shape}')
    h, w = image.shape[:2]
    cv2.imwrite(f'/tmp/oh_{name}_view.png',
                cv2.resize(image, (1400, int(h * 1400 / w))))
print('缩略图写到 /tmp/oh_*_view.png')
"
```
Expected: 三张都是 `(515, 4255, 3)`。逐张打开 `/tmp/oh_*_view.png` 确认：

- `stitch.png`：一条连续水道，左端到右端泳道线不断裂、不错位；接缝处（约横向
  30% 位置）泳道浮标连续。
- `heat.png`：只有两个色块（红=cam5 在左、绿=cam6 在右），中间一条竖直过渡带；
  **不应**出现水平方向的锯齿或斜向分界 —— 那说明 `seam_weights` 的竖直缝判据失效。
- `grid.png`：三角网格与底图内容对齐。

- [ ] **Step 11: 端到端 —— 参考贴图与真实首帧静图**

```bash
DATA4K=/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K
.venv/bin/python -m python.stitch overhead tex,still --real --video-dir "$DATA4K"
.venv/bin/python -c "
import cv2
for name in ('stitch_real', 'heat_real'):
    image = cv2.imread(f'outputs/overhead/{name}.png')
    print(f'{name:12s} {image.shape}')
    h, w = image.shape[:2]
    cv2.imwrite(f'/tmp/oh_{name}_view.png',
                cv2.resize(image, (1400, int(h * 1400 / w))))
"
```
Expected:
```
=== overhead: tex ===
  cam5      <- 20260629_172532_cam5.mp4
  cam6      <- 20260629_172532_cam6.mp4
wrote 2 reference textures -> .../outputs/overhead/ref_tex
=== overhead: still ===
canvas 4255x515 @ 170.00px/m
wrote still .../outputs/overhead/stitch_real.png
...
stitch_real  (515, 4255, 3)
heat_real    (515, 4255, 3)
```
打开 `/tmp/oh_stitch_real_view.png`：应看到真实泳池画面（无黄色标定线），接缝处水面
纹理连续。这是关键一步 —— **它验证「贴图↔相机」的位置对应是对的**：若 cam5/cam6 弄反，
接缝会出现明显错位（探索阶段已渲图确认过反向的样子）。

- [ ] **Step 12: 端到端 —— 离线拼接视频**

```bash
DATA4K=/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K
.venv/bin/python -m python.stitch overhead video \
  --video-dir "$DATA4K" --seconds-float 10
```
Expected（关键是第二行的 `NO time alignment`）：
```
=== overhead: video ===
canvas 4255x515 @ 170.00px/m -> output 4255x515 (bottom crop 0px)
NO time alignment: reading every clip from frame 0 (base 29.97fps)
299 output frames (~10.0s)
  100/299  ... fps
  200/299  ... fps
wrote video .../outputs/overhead/stitch.mp4: 299 frames in ...s -> ... fps
```
4K 逐帧 remap 很慢（预期个位数 fps），10 秒片段约需数十秒到几分钟。

- [ ] **Step 13: 抽帧检查视频**

Run:
```bash
.venv/bin/python -c "
import cv2
cap = cv2.VideoCapture('outputs/overhead/stitch.mp4')
print('fps', round(cap.get(cv2.CAP_PROP_FPS), 3),
      'frames', int(cap.get(cv2.CAP_PROP_FRAME_COUNT)),
      'size', int(cap.get(3)), 'x', int(cap.get(4)))
cap.set(cv2.CAP_PROP_POS_FRAMES, 150)
ok, frame = cap.read()
cap.release()
assert ok, 'cannot read mid frame'
h, w = frame.shape[:2]
cv2.imwrite('/tmp/oh_video_f150.png', cv2.resize(frame, (1400, int(h*1400/w))))
print('mid frame ->', frame.shape)
"
```
Expected: `size 4256 x 516`（h264 的 `yuv420p` 要求偶数宽高，ffmpeg 的 `pad` 各补 1px），
`frames` 约 299。打开 `/tmp/oh_video_f150.png` 确认运动员横跨接缝时不断裂。

- [ ] **Step 14: 提交**

```bash
git add .gitignore tests/python/test_stitch.py
git commit -F - <<'EOF'
feat(overhead): file the designer's 002.fbx and stitch it offline

Extraction, still, reference textures and the offline mp4 all run through the
shared code with only the profile changed — no geometry work was needed, because
002.fbx covers the same 25.000m x 3.000m lane as the 16 underwater planes.

The .gitignore change is not cosmetic: inputs/*.fbx and inputs/*.fbm/ were
ignoring the loose drop at inputs/002.fbx, and moving it under
inputs/overhead/models/ escapes those globs (* does not cross a separator), so
the 6.8MB asset would have become committable. inputs/overhead/models/ now
matches how inputs/underwater/models/ is handled.

The extraction test asserts the mesh order (Plane002 then Plane001) rather than
just the count, because that order is what pins cam5 to 05-02.jpg and cam6 to
C06.jpg — the mapping is positional now, so a silent reordering would swap two
cameras and show up only as a mis-registered seam.
EOF
```

---

## Task 8: overhead 实时链路

**Files:**
- Test: `tests/python/test_stitch.py`（追加 `OverheadAssetTest`）
- 产物（均不入库）：`build/assets/generated/overhead.swasset`、`overhead.stamp`、
  `inputs/configs/overhead_metal.conf`、`outputs/overhead/realtime.jsonl`

**Interfaces:**
- Consumes: Task 7 的 `outputs/overhead/mesh.json`；Task 5 的 `step_asset` /
  `step_build` / `step_run`；Task 6 的 dispatcher。
- Produces: 无新符号。C++ 侧零改动（`kMaxCameras=16` 容得下 2 路；相机身份取自 config 的
  `source.<id>` 声明顺序）。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/python/test_stitch.py` 末尾：

```python
class OverheadAssetTest(unittest.TestCase):
    """The compiled asset must carry the profile's camera ids in mesh order, and
    the two lines must not collide in the generated directory."""

    def test_camera_ids_are_written_in_mesh_order(self):
        import tempfile
        from python.assets.asset_format import CAMERA, HEADER
        from python.assets.compile_runtime_asset import compile_asset
        from python.stitch import profiles

        overhead = profiles.get("overhead")
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            asset = td / "overhead.swasset"
            # real proportions: 10m and 17.5m planes overlapping 2.5m, 3m tall
            mesh = _two_plane_json(td, [
                _plane("Plane002", "05-02.jpg", 0.0, 10.0, 0.0, 3.0),
                _plane("Plane001", "C06.jpg", 7.5, 17.5, 0.0, 3.0)])
            compile_asset(mesh, asset, overhead.camera_ids,
                          overhead.ppm, neg_v=False,
                          blend_px=overhead.blend_px, clip_uv=overhead.clip_uv,
                          source_size=overhead.source_size,
                          crop_bottom=overhead.crop_bottom)

            data = asset.read_bytes()
            header = HEADER.unpack_from(data, 0)
            self.assertEqual(header[7], 2)                 # camera_count
            ids = []
            for index in range(header[7]):
                record = CAMERA.unpack_from(data, header[2] + index * CAMERA.size)
                ids.append(record[0].split(b"\0")[0].decode())
            self.assertEqual(ids, ["cam5", "cam6"])

    def test_two_lanes_are_well_within_the_runtime_ceiling(self):
        # kMaxCameras is 16, sized for the underwater panorama; a two-lane line
        # needs no C++ change at all.
        from python.stitch import profiles

        ceiling = 16
        for name in profiles.names():
            self.assertLessEqual(len(profiles.get(name).camera_ids), ceiling)
```

- [ ] **Step 2: 运行测试确认通过或失败**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.OverheadAssetTest -v 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 2 tests in ...s
OK
```
这两条测的是既有编译器加 Task 2 的 profile，本身不需要新代码 —— 它们是回归守卫，
用来锁住「相机 ID 按 mesh 顺序写入」这个位置对应关系。

- [ ] **Step 3: 编译 overhead 资产**

Run:
```bash
.venv/bin/python -m python.stitch overhead asset
```
Expected:
```
=== overhead: asset ===
$ ... -m python.assets.compile_runtime_asset .../outputs/overhead/mesh.json \
    .../build/assets/generated/overhead.swasset --camera-ids cam5 cam6 \
    --ppm 170.0 --no-neg-v --blend-px 85.0 --crop-bottom none \
    --source-size 3840 2160 --clip-uv
2 cameras, canvas 4251x511 - crop 0 -> logical 4251x511 -> encoded 4252x512

done.
```

- [ ] **Step 4: 核对资产头部与相机表**

Run:
```bash
.venv/bin/python -c "
from python.assets.asset_format import CAMERA, HEADER
data = open('build/assets/generated/overhead.swasset', 'rb').read()
header = HEADER.unpack_from(data, 0)
print('magic', header[0], 'version', header[1])
print(f'logical {header[3]}x{header[4]}  encoded {header[5]}x{header[6]}  '
      f'cameras {header[7]}')
for index in range(header[7]):
    record = CAMERA.unpack_from(data, header[2] + index * CAMERA.size)
    print(f'  [{index}] id={record[0].split(bchr(0))[0].decode():8s} '
          f'node={record[1].split(bchr(0))[0].decode():10s} '
          f'verts={record[2]:4d} indices={record[3]:4d} '
          f'weight=({record[4]},{record[5]}) {record[6]}x{record[7]}')
print('file', len(data), 'bytes')
".replace('bchr(0)', "b'\\\\0'")
```

若上面的引号嵌套在你的 shell 里别扭，用等价的两行：

```bash
cat > /tmp/oh_asset_dump.py <<'PY'
from python.assets.asset_format import CAMERA, HEADER
data = open('build/assets/generated/overhead.swasset', 'rb').read()
header = HEADER.unpack_from(data, 0)
print('magic', header[0], 'version', header[1])
print(f'logical {header[3]}x{header[4]}  encoded {header[5]}x{header[6]}  '
      f'cameras {header[7]}')
for index in range(header[7]):
    record = CAMERA.unpack_from(data, header[2] + index * CAMERA.size)
    camera_id = record[0].split(b'\0')[0].decode()
    node = record[1].split(b'\0')[0].decode()
    print(f'  [{index}] id={camera_id:8s} node={node:10s} '
          f'verts={record[2]:4d} indices={record[3]:4d} '
          f'weight=({record[4]},{record[5]}) {record[6]}x{record[7]}')
print('file', len(data), 'bytes')
PY
.venv/bin/python /tmp/oh_asset_dump.py
```
Expected:
```
magic b'SW4KAST\x00' version 1
logical 4251x511  encoded 4252x512  cameras 2
  [0] id=cam5     node=Plane002   verts=  84 indices= 360 weight=(0,0) 1531x511
  [1] id=cam6     node=Plane001   verts= 148 indices= 612 weight=(1446,0) 2805x511
file 4439352 bytes
```
关键是 `cam5` 配 `Plane002`、`cam6` 配 `Plane001` —— 与 Task 7 的 mesh 顺序一致，
且两块权重的横向范围有重叠（1446 < 1531），正是那条竖直接缝。

- [ ] **Step 5: 确认 stamp 写出且带 profile 名**

Run:
```bash
cat build/assets/generated/overhead.stamp; echo
ls -la build/assets/generated/*.stamp
```
Expected: 内容以 `overhead ` 开头，含 `--ppm 170.0`、`--blend-px 85.0`、
`--crop-bottom none`、`--source-size 3840 2160`、`--clip-uv`。

- [ ] **Step 6: 确认重跑会跳过**

Run:
```bash
.venv/bin/python -m python.stitch overhead asset
```
Expected:
```
=== overhead: asset ===
asset up to date: .../build/assets/generated/overhead.swasset

done.
```

- [ ] **Step 7: 确认改 shaping 参数会重编**

Run:
```bash
.venv/bin/python -m python.stitch overhead asset --blend-px 0 2>&1 | tail -3
.venv/bin/python -m python.stitch overhead asset 2>&1 | tail -3
```
Expected: 第一条重新编译（打印 `2 cameras, canvas ...`），第二条又重新编译
（因为 stamp 现在记的是 `--blend-px 0.0`，与 profile 的 85.0 不符）。这证明 stamp
机制对 overhead 生效 —— 改参数不会被 mtime 误判为「已是最新」。

- [ ] **Step 8: 构建 swim_realtime**

Run:
```bash
.venv/bin/python -m python.stitch overhead build
```
Expected: 若 `build/metal-release/swim_realtime` 已存在，打印
`executable present: .../swim_realtime`；否则跑 cmake configure + build，最后无错误退出。
首次构建需数分钟。

- [ ] **Step 9: 实时跑 10 秒（无窗口，先确认能起来）**

```bash
DATA4K=/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K
.venv/bin/python -m python.stitch overhead live \
  --video-dir "$DATA4K" --seconds 10 --no-window
```
Expected: 先打印 `wrote config .../inputs/configs/overhead_metal.conf (2 lanes, 0 with a start offset)`
——`0 with a start offset` 正是 `sync="none"` 的体现，且**不应**出现
`no wall-clock alignment` 那行提示。随后 `swim_realtime` 每秒刷一行
render/decode/preview FPS，10 秒后正常退出，最后打印 `metrics -> .../realtime.jsonl`。

- [ ] **Step 10: 核对生成的 config**

Run:
```bash
cat inputs/configs/overhead_metal.conf
```
Expected: 恰好两条 `source.` 行（`source.cam5=` 与 `source.cam6=`，按此顺序），
`asset=` 指向 `overhead.swasset`，`metrics=` 指向 `outputs/overhead/realtime.jsonl`，
**没有**任何 `start_ms` 行。

- [ ] **Step 11: 核对实时指标**

Run:
```bash
.venv/bin/python -c "
import json
from pathlib import Path
lines = Path('outputs/overhead/realtime.jsonl').read_text().splitlines()
print('intervals', len(lines))
last = json.loads(lines[-1])
for key in sorted(last):
    value = last[key]
    if isinstance(value, (int, float, str, bool)) or value is None:
        print(f'  {key} = {value!r}')
"
```
Expected: 至少若干条 interval 记录；最后一条里渲染帧率接近 30（4K 两路解码比 16 路
720p 重，若明显低于 30 需记录实测值而不是当作失败 —— 这是本轮第一次跑 4K 实时）。

- [ ] **Step 12: 带窗口跑一次，目视确认画面**

```bash
DATA4K=/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K
.venv/bin/python -m python.stitch overhead live \
  --video-dir "$DATA4K" --seconds 15
```
Expected: 弹出预览窗口，显示一条横向水道全景（4252×512 的宽幅），运动员游过接缝时
不断裂。目视确认后窗口自行关闭。

- [ ] **Step 13: 确认水下实时链路未被破坏**

```bash
UWDIR=/Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-xlj-under-videos/swb_20260728-150356_6
.venv/bin/python -m python.stitch underwater extract,asset --force 2>&1 | tail -4
.venv/bin/python -m python.stitch underwater live \
  --video-dir "$UWDIR" --seconds 10 --no-window 2>&1 | grep -E "wrote config|align window|lane skew"
```
Expected: asset 打印 `16 cameras, canvas 6001x721 - crop 65 -> logical 6001x656`；
live 打印 `wall-clock align window 30.000s; lane skew ...ms` 与
`wrote config ... (16 lanes, N with a start offset)`，其中 N > 0 —— 水下的墙钟对齐
必须仍然生效。

- [ ] **Step 14: 跑全量测试**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 218 tests in ...s
OK
```
216（Task 7 后）+ 2 = 218。

- [ ] **Step 15: 清理并提交**

```bash
rm -f /tmp/oh_asset_dump.py /tmp/oh_*_view.png /tmp/oh_video_f150.png
git add tests/python/test_stitch.py
git commit -F - <<'EOF'
feat(overhead): compile and run the two-lane asset on Metal

No C++ changed. kMaxCameras is already 16 and camera identity already comes from
the config's source.<id> declaration order, so a two-lane line is data the
runtime accepts as-is.

The asset test asserts the ids land in mesh order (cam5 on Plane002, cam6 on
Plane001) rather than merely that two ids exist: the texture-to-camera mapping is
positional now, so a reordering would silently swap the two cameras and only
surface as a mis-registered seam in the rendered panorama.

Verified that the shaping stamp works for this line too — recompiling with
--blend-px 0 and then without it rebuilds both times, so a changed parameter is
never mistaken for an up-to-date asset.
EOF
```

---

## Task 9: README 与收尾

**Files:**
- Modify: `README.md`（「水下拼接」一节改写为「平面拼接」；目录树；已知限制）
- Test: `tests/python/test_stitch.py`（追加 `DocsTest`）

**Interfaces:**
- Consumes: 全部前置任务。
- Produces: 无新符号。

- [ ] **Step 1: 写失败的测试**

追加到 `tests/python/test_stitch.py` 末尾：

```python
class DocsTest(unittest.TestCase):
    """The README must not point at commands that no longer exist."""

    def test_readme_has_no_retired_commands(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        for retired in ("uw-extract", "uw-tex", "uw-render", "uw-real",
                        "uw-video", "run_underwater.sh", "run_underwater.ps1",
                        "python.underwater", "underwater_16.swasset"):
            self.assertNotIn(retired, readme, f"README still mentions {retired}")

    def test_readme_documents_both_stitch_lines(self):
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("run_stitch.sh", readme)
        self.assertIn("overhead", readme)
        self.assertIn("002.fbx", readme)
```

- [ ] **Step 2: 运行测试确认失败**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.DocsTest -v 2>&1 | grep -E "README still|^(Ran|OK|FAILED)"
```
Expected: `test_readme_has_no_retired_commands` FAIL，报出第一个仍被提及的旧命令。

- [ ] **Step 3: 改 README 的目录树**

`README.md` 第 176-183 行的 `scripts/` 部分：

```text
├── scripts/
│   ├── run_metal.sh              # demo / benchmarks / soak
│   ├── run_python.sh             # still / 4k / keypoint / extract / bake / asset / we-*
│   ├── run_stitch.sh             # 平面拼接统一入口（macOS / Linux）
│   ├── run_stitch.ps1            # 同上（Windows）
│   ├── run_win.ps1               # Windows 六路实时启动器
│   ├── run_win.bat               # 同上（cmd 包装）
│   └── run_water_entry.sh        # 入水检测机位难例筛选全流程
```

同时在 `inputs/` 部分补上新目录（第 128-137 行附近）：

```text
├── inputs/
│   ├── pool/models/pool.fbx
│   ├── pool/textures/camera_[1-6]_composite.png
│   ├── underwater/models/all.fbx + all.fbm/     # 16 块水下平面（本地，未入库）
│   └── overhead/models/002.fbx + 002.fbm/       # 2 块俯视平面（本地，未入库）
```

- [ ] **Step 4: 用新一节替换 README 的「水下拼接」整节**

把 `README.md` 第 376 行 `## 水下拼接（underwater stitch）` 起、到第 459 行
（`## 入水检测机位（water entry）` 之前）的整节替换为：

````markdown
## 平面拼接（stitch）

`python/stitch/` 实现「N 块平面横向一字排开」的拼接通路。它不复制算法，而是 import 复用
pool 的提取与渲染函数（`python.assets.extract_fbx`、`python.validation.reference_renderer`），
产物按线路分目录写入 `outputs/<profile>/`，互不交叉。

两条线路的差异全部是 `python/stitch/profiles.py` 里的数据，代码不分叉：

| | underwater | overhead |
| --- | --- | --- |
| 模型 | `all.fbx`（16 块，含杂物需过滤） | `002.fbx`（2 块，干净） |
| 相机 | `underA16` … `underA1` | `cam5`、`cam6` |
| 片段 | `*_underAi.ts` | `*_camN.mp4` |
| 源尺寸 | 1280×720 | 3840×2160 |
| 每米像素 | 240 | 170 |
| 接缝过渡 | 120 px（0.5 m） | 85 px（0.5 m） |
| 时间对齐 | manifest 墙钟 | 无（同一 PTP 同步域，帧级偏差） |
| 静图 | 缩回源高 360（3278×360） | 原生 ppm（4255×515） |
| 资产 | `underwater.swasset`（6001×656） | `overhead.swasset`（4251×511） |

两条线路覆盖的是**同一条 25.000 m × 3.000 m 水道**，一个从水下看、一个从水上看 ——
这是同一套几何代码能服务两者的原因，也是 overhead 变体存在的目的：让水上视角与水下
16 路关注同一名运动员。

`overhead` 的两张贴图 `05-02.jpg` / `C06.jpg` 是设计师在 overhead5 / overhead6 机位标定
的帧。用 SIFT + RANSAC 与 `20260629-4K` 的 cam5 / cam6 首帧配准，内点 99 / 187、单应近似
恒等（四角位移均值 2.9 px / 0.3 px），确认为同一对物理相机 —— 所以视频侧用这两路 4K。
overhead5 / overhead6 自身目前只有 50 组快照 jpg，没有原始视频。

### 统一入口

```bash
./scripts/run_stitch.sh PROFILE STEPS [选项…]      # macOS / Linux
pwsh scripts/run_stitch.ps1 PROFILE STEPS [选项…]  # Windows
```

七个步骤，逗号分隔、按给出顺序执行：

| 步骤 | 作用 |
| --- | --- |
| `extract` | FBX → `outputs/<profile>/mesh.json` |
| `tex` | 导出每台相机的参考贴图 `ref_tex/<camera>.png`（首帧，无标定线） |
| `still` | 静图 + 网格诊断图 + 融合热图；`--real` 用参考贴图，产物加 `_real` 后缀 |
| `video` | 每路片段 → 全景 mp4 |
| `asset` | mesh JSON → GPU `.swasset` |
| `build` | 构建 `swim_realtime` |
| `live` | 实时拼接（预览 / HEVC / 指标） |

`extract` / `tex` / `asset` 的产物比输入新时跳过（`asset` 另比对 shaping 参数的 stamp），
`--force` 强制重做；`still` / `video` 每次都渲 —— 它们的口径可从命令行覆盖，按 mtime
跳过会让人看到一张过期却像是新的图。

```bash
# 水下 16 路：一条命令跑完提取 → 编译 → 构建 → 实时
./scripts/run_stitch.sh underwater extract,asset,build,live \
  --video-dir /path/to/swb_20260728-150356_6 --seconds 30 --encode

# 俯视两路：离线静图与拼接视频
./scripts/run_stitch.sh overhead extract,still
./scripts/run_stitch.sh overhead tex,still --real \
  --video-dir /path/to/20260629-4K
./scripts/run_stitch.sh overhead video --video-dir /path/to/20260629-4K \
  --seconds-float 10

# 俯视两路：实时
./scripts/run_stitch.sh overhead extract,asset,build,live \
  --video-dir /path/to/20260629-4K --seconds 15
```

常用选项：`--seconds N`（live 时长）、`--seconds-float N`（video 时长）、`--encode`、
`--no-window`（离屏）、`--fps N`、`--blend-px N`、`--ppm N`、`--real`、`--force`、
`--config PATH`（用现成 config）、`--backend metal|d3d11|cudagl`。

平台差异全部由 Python 处理：macOS 用 Ninja + `metal` 后端 + `build/metal-release/swim_realtime`；
Windows 用 Visual Studio 17 2022 (x64) + `d3d11` 后端 +
`build/win-d3d11/Release/swim_realtime.exe`（有 CUDA/FFmpeg/GLFW 时可 `--backend cudagl`）。
运行时 config 每次按片段目录重新生成到 `inputs/configs/<profile>_<backend>.conf`，
`source.<camera>=` 的声明顺序即通道顺序。

### 分步细节

网格按每块世界 X 最小值升序排列（左→右），不依赖 FBX 节点声明顺序。相机身份由此**按位置**
对应 —— profile 的 `camera_ids` 与 mesh 顺序一一配对，不解析贴图文件名（overhead 的
`05-02.jpg` / `C06.jpg` 无从解析出 `cam5` / `cam6`）。

`all.fbx` 含全部 16 块平面，但同时夹带无纹理的支架框、泳道标记条与重复网格，所以
underwater 的 profile 打开 `planes_only`：只保留「每个纹理一块、位于泳池 Y 带内的全高
平面」。`002.fbx` 恰好只有两块平面，不需要过滤。

静图默认用标注网格图（underwater 取数据集的 `annotation-grids/`，可用 `STITCH_GRID_DIR`
或 `ANNOTATION_PREVIEW_DATASET_ROOT` 覆盖；overhead 取 `002.fbm` 里设计师的标定图）。
`--real` 换成 `tex` 导出的相机首帧。参考贴图按 `<camera_id>.png` 命名而非沿用 mesh 的
`texture_basename`：后者对 overhead 是设计师的工作文件名（看不出哪台相机），且把无损解码
写回 `.jpg` 会二次编码（实测 cam5 最大通道误差 35）。

`--full-res`（underwater 默认开）输出高度对齐源图高度、宽度等比缩放；缩放前会**自动砍掉
最下方存在黑色（无纹理）像素的整行**（矮平面的透视地面缺口），再等比缩放。overhead 两块
都是全高平面、`ppm` 已是原生密度，所以关掉。

离线拼接视频的时间轴按 profile 的 `sync` 决定。`sync="manifest"`（underwater）：各路
`.ts` 的第 0 帧不是同一时刻 —— 录制器把关键帧放在 lookback 窗口内的任意位置，GOP 粒度
使各路偏差可达数秒；按 manifest 的 `align_start_ms` 与各路 `keyframe_timestamp_ms` 换算
每路起始帧，与前端播放器同一套公式，manifest 缺失或没有 align 窗口会直接报错退出。
`sync="none"`（overhead）：4K 会话的 manifest 没有可用墙钟（`sync_summary.status` 是
`waiting_for_syncbridge_events`、`mappings[].offset_us` 全为 `null`），六路 ZCAM 同处一个
EzLink/IEEE1588 同步域、同一次录制，偏差在帧级，各路从第 0 帧读。`--no-align` 可强制
前者也不对齐（用于与旧行为对比）。

### 能力边界

pool 六路**不在** profile 注册表里，这是有意的：它的网格是**两排**（`01/02/03` 一排、
`u/Plane004/Plane007` 另一排），相机顺序 `cam3 cam2 cam1 cam4 cam5 cam6` 不是 world-X
升序，且用距离变换羽化而非竖直硬缝。把它塞进来会让 profile 长出三个只为一条线路存在的
字段。pool 继续走 `python.validation.reference_renderer` 与 `CMakeLists.txt` 里的
`pool_4k.swasset` 规则。

macOS/Metal 实测（underwater）：16 路 1280×720 MPEG-TS → 6002×656，渲染 30.1fps、解码
4848 帧零 malformed、HEVC 硬件编码 30.1fps、预览零丢帧。相机数量、相机 ID、输出尺寸、
解码分辨率全部来自 config 与 `.swasset`，三个后端（Metal / D3D11 / CUDA-GL）共用同一套
`swim_core` 逻辑。
````

- [ ] **Step 5: 补一条已知限制**

在 README 的「已知限制」段（原第 579 行起）里，把这一条：

```
- 六路输入数量必须与六块网格一致，且必须保持 `cam3 cam2 cam1 cam4 cam5 cam6` 的固定位置顺序。
```

之后插入：

```
- 拼接线路（`python/stitch/`）的相机身份是**位置**对应：profile 的 `camera_ids` 按顺序
  配 mesh（world-X 升序）。改动 FBX 里平面的相对位置、或改 profile 的 id 顺序，会把相机
  错配到别的平面上，症状是接缝处错位而不是报错。
- overhead 线路的视频侧用 `20260629-4K` 的 cam5/cam6（已用 SIFT 配准确认与设计师标定的
  overhead5/overhead6 同机位）。overhead5/overhead6 自身只有 50 组快照 jpg，没有原始视频；
  拿到后只需在 `profiles.py` 新增一条记录（同一个 `002.fbx`，换 `camera_ids` 与
  `clip_suffix`，`sync` 改回 `manifest`）。
- 4K 两路实时的解码负载明显高于 16 路 720p（像素总量约 2.4 倍），实测帧率见
  `outputs/overhead/realtime.jsonl`。
```

- [ ] **Step 6: 运行测试确认通过**

Run:
```bash
.venv/bin/python -m unittest tests.python.test_stitch.DocsTest -v 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 2 tests in ...s
OK
```

- [ ] **Step 7: 跑全量测试**

Run:
```bash
.venv/bin/python -m unittest discover -s tests/python -t . 2>&1 | grep -E "^(Ran|OK|FAILED)"
```
Expected:
```
Ran 220 tests in ...s
OK
```
216（Task 7 后）+ 2（Task 8）+ 2（Task 9）= 220。

- [ ] **Step 8: 全仓扫一遍残留引用**

Run:
```bash
grep -rn "python\.underwater\|uw-extract\|uw-render\|uw-real\|uw-video\|uw-tex\|run_underwater\|underwater_16" \
  --include='*.py' --include='*.sh' --include='*.ps1' --include='*.md' \
  --include='*.txt' --include='*.cmake' --include='CMakeLists.txt' \
  . 2>/dev/null | grep -v '^./docs/superpowers/' || echo "no stale references"
```
Expected: `no stale references`

（`docs/superpowers/` 下的历史 spec 与 plan 记录的是当时的状态，不改。）

- [ ] **Step 9: 清理临时文件**

Run:
```bash
rm -rf /tmp/probe002 /tmp/plancheck /tmp/oh_* /tmp/uw_*
ls /tmp | grep -E "probe002|plancheck|oh_|uw_" || echo "clean"
```
Expected: `clean`

- [ ] **Step 10: 确认工作区干净、无意外入库**

Run:
```bash
git status --short
git log --oneline -9
```
Expected: `git status` 只剩 README 与测试文件待提交（无 `.fbx`、`.png`、`.mp4`、
`.swasset`、`.conf`）；`git log` 能看到 Task 1-8 的八个提交。

- [ ] **Step 11: 提交**

```bash
git add README.md tests/python/test_stitch.py
git commit -F - <<'EOF'
docs(stitch): document both lines under one section

The README described a single underwater task and pointed at five uw-* commands
and two wrapper scripts that no longer exist. It now describes the shared
pipeline with a table of what the two profiles differ in, which is also the
honest summary of the refactor: fourteen values, no forked code.

A test asserts the retired names are absent. Documentation that names a command
which no longer exists is worse than no documentation, and this one had twenty
such references.

Records the two things a reader cannot infer from the code: that the overhead
textures were pinned to cam5/cam6 by SIFT registration rather than by their
filenames, and that camera identity is positional — so reordering planes in the
FBX mis-registers a seam instead of raising.
EOF
```

---

## 自检

**规格覆盖。** 逐节对照 `docs/superpowers/specs/2026-07-30-overhead-lane-stitch-design.md`：

| 规格小节 | 落在哪个任务 |
| --- | --- |
| profile：一条拼接线路的全部差异 | Task 2 |
| 目录改名 | Task 1 |
| 删除写死水下的三处（正则 / 常量 / manifest） | Task 3（正则、manifest 分支）、Task 5（常量） |
| 步骤 dispatcher | Task 6 |
| shell 入口收敛 | Task 6 |
| 能力边界：pool 不进注册表 | Task 2（模块文档串）、Task 9（README） |
| 资产命名 | Task 2（profile 字段）、Task 8（实际编译验证） |
| FBX 落位与 .gitignore | Task 7 |
| 数据流（extract→still→tex→video→asset→live） | Task 7（前四步）、Task 8（后两步） |
| 几何参数依据（ppm/blend/crop/neg_v） | Task 2（写进 profile 并附理由注释） |
| 错误处理（7 条） | Task 2（profile 未注册、clip 缺失/歧义）、Task 3（相机数不符）、Task 5（sync 分支）、Task 6（未知步骤、缺 --video-dir） |
| 测试（7 项新增） | Task 2/3/4/5/6/7/8/9 各自的测试类 |
| 验证（9 步） | Task 7 Step 8-13、Task 8 Step 3-13、Task 9 Step 7-8 |
| 后续扩展成本 | Task 9（README 已知限制） |
| README | Task 9 |

规格里「水下静图逐像素不变」这条回归要求，在 Task 1 Step 12、Task 4 Step 8、
Task 6 Step 13 各验证一次（改名后、改贴图命名后、走新入口后）。

**占位符扫描。** 无 TBD / TODO / 「类似 Task N」/ 「适当处理错误」。每个改代码的步骤
都给了完整代码块；每个跑命令的步骤都给了预期输出。

**类型一致性。**

- `write_config(profile, path, video_dir, backend, encode_path, align=True)` ——
  Task 5 定义，Task 5 Step 1 的三处测试调用与 Task 6 的 `realtime.step_run` 间接调用一致。
- `render_video(data_path, video_dir, out_path, camera_ids, clip_for, ...)` ——
  Task 3 定义，Task 6 `step_video` 按此调用（`camera_ids=`、`clip_for=` 均关键字传参）。
- `export_ref_tex.export(profile, out_dir=None, video_dir=None)` 与
  `tex_names(profile)` —— Task 4 定义，Task 6 `step_tex` / `step_still` 按此调用。
- `render_stills(..., tex_names=None)` —— Task 4 加参数，Task 6 `step_still` 使用。
- 四个 `step_*(profile, args)`（Task 5）与三个新 step（Task 6）签名一致，
  Task 6 的 `test_every_step_takes_profile_and_args` 用 `inspect.signature` 锁住。
- `StepError` 单一来源 `profiles`（Task 2），`run.py` 重新导出（Task 5），
  Task 4/6 从 `profiles` 导入 —— 全仓一个类，不存在两个同名异物。
- `profile.mesh_json` / `ref_tex_dir` / `metrics` / `config_path()` / `clip_for()` ——
  Task 2 定义，Task 3/4/5/6 引用的名字与之逐字相同。

**测试计数账。** 180（基线）→ 192（+12 Task 2）→ 194（−1 +3 Task 3）→ 200（+6 Task 4）
→ 206（+6 Task 5）→ 213（+7 Task 6）→ 216（+3 Task 7）→ 218（+2 Task 8）→ 220（+2 Task 9）。

