# Swim FBX Demo Project Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate project inputs, outputs, source code, scripts, and documentation; repair imports and paths; and provide a runnable Chinese README without deleting historical render artifacts.

**Architecture:** Keep the project as directly executable Python scripts instead of introducing a package. Each Python entry derives stable defaults from its file location, while the shell entry derives the project root from its own location and reads the external 4K dataset through one overridable environment variable.

**Tech Stack:** Python 3.10, Autodesk FBX Python SDK, NumPy, OpenCV, Bash, FFmpeg, Markdown.

## Global Constraints

- Preserve every existing PNG, MP4, and log byte-for-byte; move them only.
- Keep `.venv` at the project root and do not rebuild it.
- Keep the 4K dataset outside the project at `/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K` and do not modify it.
- Keep camera order exactly `cam3 cam2 cam1 cam4 cam5 cam6`.
- Do not change FBX geometry, UV behavior, compositing behavior, FFmpeg codec, preset, or CRF.
- Do not render the complete 601-second result during verification.
- The current directory has no Git metadata, so commit steps are unavailable; use the verification checkpoint at the end of each task instead.

## File Structure

- Create `README.md`: Chinese project entry document and commands.
- Rename `FbxCommon.py` to `src/fbx_common.py`: Autodesk FBX load/save helpers.
- Move and modify `src/bake_uv.py`: bake center-line UV extension into an FBX.
- Move and modify `src/extract_fbx.py`: extract FBX mesh data into portable JSON metadata.
- Move and modify `src/render_pool.py`: render stills and videos from mesh data.
- Rename and modify `scripts/run_4k.sh`: reproducible 4K rendering entry point.
- Move project-owned source assets to `inputs/models/` and `inputs/textures/`.
- Move derived data and historical artifacts to `outputs/data/`, `outputs/images/`, `outputs/videos/`, and `outputs/logs/`.
- Keep design and plan documents under `docs/superpowers/`.

---

### Task 1: Preserve and Relocate Existing Files

**Files:**
- Move: `FbxCommon.py` → `src/fbx_common.py`
- Move: `bake_uv.py` → `src/bake_uv.py`
- Move: `extract_fbx.py` → `src/extract_fbx.py`
- Move: `render_pool.py` → `src/render_pool.py`
- Move: `run.sh` → `scripts/run_4k.sh`
- Move: `pool.fbx` → `inputs/models/pool.fbx`
- Move: `textures/*.png` → `inputs/textures/*.png`
- Move: `pool_mesh.json` → `outputs/data/pool_mesh.json`
- Move: `pool.png`, `pool_grid.png`, `pool_grid_preview.png` → `outputs/images/`
- Move: `*.mp4` → `outputs/videos/`
- Move: `pool_4k_full.log` → `outputs/logs/pool_4k_full.log`
- Delete: `.DS_Store`

**Interfaces:**
- Consumes: the exact root-level files inventoried in the design.
- Produces: the approved directory tree without changing asset contents.

- [ ] **Step 1: Record byte sizes and SHA-256 hashes before moving**

Run a read-only inventory over `pool.fbx`, all texture PNGs, all root PNGs, all MP4s, the log, and `pool_mesh.json`. Save the output outside the project in a temporary file so the final root remains clean.

```bash
find . -maxdepth 2 -type f \( -name '*.fbx' -o -name '*.json' -o -name '*.png' -o -name '*.mp4' -o -name '*.log' \) -print0 \
  | sort -z \
  | xargs -0 shasum -a 256 > /tmp/swim_fbx_demo-before.sha256
```

Expected: 16 entries: 1 FBX, 1 mesh JSON, 9 PNGs, 4 MP4s, and 1 log.

- [ ] **Step 2: Verify the target structure is absent before the move**

Run:

```bash
test ! -e inputs/models/pool.fbx
test ! -e outputs/videos/pool_4k_full.mp4
test ! -e src/render_pool.py
```

Expected: all commands exit 0.

- [ ] **Step 3: Create directories and move files with filesystem rename operations**

Create exactly these directories:

```text
inputs/models
inputs/textures
outputs/data
outputs/images
outputs/videos
outputs/logs
src
scripts
```

Move files according to the mapping above. Use filesystem `mv` for the 14 GB media files so they are renamed in place rather than copied or rewritten. Remove only `.DS_Store`.

- [ ] **Step 4: Preserve script executability**

Run:

```bash
chmod +x scripts/run_4k.sh
```

Expected: `test -x scripts/run_4k.sh` exits 0.

- [ ] **Step 5: Verify every asset arrived unchanged**

Generate the new-path inventory, normalize old and new path prefixes to basenames for comparison, and compare hashes. Explicitly compare the four MP4 byte sizes with their captured pre-move values.

Expected: every historical FBX, PNG, MP4, and log hash matches; only `outputs/data/pool_mesh.json` may change later when portable metadata is regenerated.

---

### Task 2: Align Python Imports, Defaults, and Portable JSON Paths

**Files:**
- Modify: `src/bake_uv.py`
- Modify: `src/extract_fbx.py`
- Modify: `src/render_pool.py`
- Modify: `outputs/data/pool_mesh.json` by regeneration

**Interfaces:**
- Consumes: `PROJECT_ROOT = Path(__file__).resolve().parents[1]` in each entry script.
- Produces: default paths rooted at `inputs/` and `outputs/`; `extract_fbx.py` retains positional `src` and `dst` compatibility and adds `--tex-dir`.
- Produces: JSON `source` and `texture` strings that are project-relative whenever their files live under the project root.

- [ ] **Step 1: Run pre-change behavior checks and confirm they fail**

From `/tmp`, run:

```bash
/Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/.venv/bin/python \
  /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/src/extract_fbx.py --help
/Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/.venv/bin/python \
  /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/src/render_pool.py
```

Expected before implementation: at least one command fails because `FbxCommon` or a current-working-directory-relative default cannot be resolved.

- [ ] **Step 2: Normalize the FBX helper import and path types**

In `src/bake_uv.py` and `src/extract_fbx.py`, replace:

```python
import FbxCommon
```

with:

```python
import fbx_common
```

and replace calls with `fbx_common.InitializeSdkObjects()`, `fbx_common.LoadScene(...)`, and `fbx_common.SaveScene(...)`.

Add to each entry script:

```python
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUTS_DIR = PROJECT_ROOT / "inputs"
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
```

- [ ] **Step 3: Make `bake_uv.py` location-independent and validate inputs**

Keep `src` and `dst` as required positional arguments. Change `--tex-dir` to `type=Path` with default `INPUTS_DIR / "textures"`. Convert the positional paths to `Path`, require the source file and texture directory to exist, create `dst.parent`, and pass strings to the FBX SDK.

Use this texture-load guard before reading `.shape`:

```python
texture_path = tex_dir / name
texture = cv2.imread(str(texture_path), cv2.IMREAD_GRAYSCALE)
if texture is None:
    raise SystemExit(f"cannot read texture: {texture_path}")
th = texture.shape[0]
```

- [ ] **Step 4: Give `extract_fbx.py` a compatible argparse interface**

Replace manual `sys.argv` parsing with:

```python
ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("src", nargs="?", type=Path, default=INPUTS_DIR / "models" / "pool.fbx")
ap.add_argument("dst", nargs="?", type=Path, default=OUTPUTS_DIR / "data" / "pool_mesh.json")
ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "textures")
args = ap.parse_args()
```

Use one helper for portable metadata:

```python
def display_path(path):
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)
```

Pass `tex_dir` through `walk()` and `extract_mesh()`. Set each mesh's `texture` to `display_path(tex_dir / texture_basename)` when a texture basename exists, otherwise keep it `None`; set top-level `source` to `display_path(src)`, create the destination parent, and write UTF-8 JSON.

- [ ] **Step 5: Make `render_pool.py` defaults location-independent**

Use these argparse defaults and types:

```python
ap.add_argument("--data", type=Path, default=OUTPUTS_DIR / "data" / "pool_mesh.json")
ap.add_argument("--tex-dir", type=Path, default=INPUTS_DIR / "textures")
ap.add_argument("--still", type=Path, default=None)
ap.add_argument("--grid-still", type=Path, default=None)
ap.add_argument("--videos", nargs="+", type=Path, default=None)
ap.add_argument("--video", type=Path, default=None)
```

Before processing, require the data file. For still rendering, load each texture with `cv2.imread(str(path))`, stop with `cannot read texture: <path>` if it returns `None`, and create each output parent. For video rendering, require `ffmpeg` via `shutil.which("ffmpeg")`, require every source video, create the output parent, and convert `Path` objects to strings at OpenCV, JSON, and subprocess boundaries.

- [ ] **Step 6: Regenerate portable mesh JSON**

Run from `/tmp`:

```bash
/Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/.venv/bin/python \
  /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/src/extract_fbx.py
```

Expected: exit 0, six mesh summaries, and `wrote .../outputs/data/pool_mesh.json`.

Check:

```bash
jq '{source, textures: [.meshes[].texture]}' outputs/data/pool_mesh.json
```

Expected: `source` is `inputs/models/pool.fbx`; all six textures start with `inputs/textures/`; no `/Users/` string appears in the JSON.

- [ ] **Step 7: Run Python checkpoint**

Run:

```bash
.venv/bin/python -m compileall -q src
(cd /tmp && /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/.venv/bin/python /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/src/bake_uv.py --help)
(cd /tmp && /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/.venv/bin/python /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/src/extract_fbx.py --help)
(cd /tmp && /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/.venv/bin/python /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/src/render_pool.py)
```

Expected: all commands exit 0; the renderer prints canvas dimensions without creating output.

---

### Task 3: Align the 4K Shell Entry Point

**Files:**
- Modify: `scripts/run_4k.sh`

**Interfaces:**
- Consumes: optional positional `seconds` and optional positional output path.
- Consumes: optional `SWIMMING_DATASET_DIR` override.
- Produces: an H.264 MP4 through `src/render_pool.py`, defaulting under `outputs/videos/`.

- [ ] **Step 1: Confirm the old script does not honor the new dataset override**

Run `bash -n scripts/run_4k.sh`, then inspect for `SWIMMING_DATASET_DIR`.

Expected before implementation: syntax passes, but the environment variable is absent and the old `/Users/penghaotian/Downloads/20260626/20260629游泳4K` path is still present.

- [ ] **Step 2: Replace root, dataset, and output setup**

Use:

```bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SECONDS_ARG="${1:-10}"
DATASET_DIR="${SWIMMING_DATASET_DIR:-/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K}"
SESSION="20260629_172532"
OUT="${2:-$PROJECT_ROOT/outputs/videos/pool_4k_test${SECONDS_ARG}s.mp4}"
PY="$PROJECT_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
```

Build a Bash array in exact mesh order:

```bash
VIDEOS=(
  "$DATASET_DIR/${SESSION}_cam3.mp4"
  "$DATASET_DIR/${SESSION}_cam2.mp4"
  "$DATASET_DIR/${SESSION}_cam1.mp4"
  "$DATASET_DIR/${SESSION}_cam4.mp4"
  "$DATASET_DIR/${SESSION}_cam5.mp4"
  "$DATASET_DIR/${SESSION}_cam6.mp4"
)
```

Require the dataset directory and each video before creating output. Create `dirname "$OUT"`, then invoke the absolute renderer and mesh JSON paths.

- [ ] **Step 3: Verify syntax and missing-input behavior**

Run:

```bash
bash -n scripts/run_4k.sh
SWIMMING_DATASET_DIR=/tmp/does-not-exist scripts/run_4k.sh 0.1 /tmp/should-not-exist.mp4
```

Expected: syntax exits 0; the second command exits nonzero with `dataset directory not found: /tmp/does-not-exist`; no output file is created.

---

### Task 4: Write the Project README

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: final directory and CLI behavior from Tasks 1–3.
- Produces: copy-pasteable commands run from the project root.

- [ ] **Step 1: Verify the entry document is absent**

Run:

```bash
test ! -e README.md
```

Expected before implementation: exit 0.

- [ ] **Step 2: Write the Chinese README**

Include these sections with exact final paths and commands:

```text
项目简介
处理流程
目录结构
环境依赖
快速开始
重新提取 FBX 网格
生成静态图和网格预览
渲染 4K 测试片和全长视频
输入顺序与输出说明
已知限制
```

Document Python 3.10, Autodesk FBX Python SDK, NumPy, OpenCV, and FFmpeg. State that the existing `.venv` is macOS/Python 3.10-specific. Show `SWIMMING_DATASET_DIR` override and the fixed camera order. Explain that frame rates are aligned to the lowest source FPS but capture start times are not synchronized by the renderer.

- [ ] **Step 3: Check every documented local path**

Run `rg` over README paths and verify each referenced source, input, output directory, and script exists. Run every `--help` command copied into the README.

Expected: no root-level legacy names such as `render_pool.py`, `pool_mesh.json`, `textures/`, or `run.sh` appear as executable paths.

---

### Task 5: End-to-End Verification and Final Inventory

**Files:**
- Verify: all final files
- Create temporarily outside project: a low-resolution PNG and a very short MP4

**Interfaces:**
- Consumes: the complete organized project.
- Produces: evidence that paths, FBX extraction, still rendering, and 4K rendering work without altering historical outputs.

- [ ] **Step 1: Verify imports**

Run:

```bash
PYTHONPATH=src .venv/bin/python -c 'import fbx_common, fbx, cv2, numpy; print("imports ok")'
```

Expected: `imports ok`.

- [ ] **Step 2: Render a low-resolution still from outside the project**

Run:

```bash
(cd /tmp && /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/.venv/bin/python \
  /Users/penghaotian/Documents/pythonCode/temp2025.6/probe_work/swim_fbx_demo/src/render_pool.py \
  --ppm 5 --still /tmp/swim_fbx_demo-smoke.png)
```

Expected: exit 0, a non-empty PNG around 251 × 106 pixels, and no new project output.

- [ ] **Step 3: Render a very short 4K smoke video**

Run:

```bash
scripts/run_4k.sh 0.1 /tmp/swim_fbx_demo-smoke.mp4
ffprobe -v error -show_entries stream=codec_name,width,height -of compact /tmp/swim_fbx_demo-smoke.mp4
```

Expected: exit 0; H.264 video at 5002 × 2102; the command processes only about two frames.

- [ ] **Step 4: Check the final root and legacy references**

Run:

```bash
find . -maxdepth 1 -mindepth 1 -print | sort
rg -n '/Users/penghaotian/Downloads/20260626|import FbxCommon|"textures"|"mesh.json"' src scripts README.md
```

Expected: root contains `.venv`, `README.md`, `docs`, `inputs`, `outputs`, `scripts`, and `src`; the legacy-reference search has no matches except historical explanation deliberately quoted in documentation.

- [ ] **Step 5: Reconfirm historical artifact integrity**

Compare the post-move hashes against `/tmp/swim_fbx_demo-before.sha256` by basename. Exclude regenerated `pool_mesh.json` from byte-equality, then assert its mesh count is six and each mesh retains the expected node, texture basename, UV set, and triangle count.

Expected: all FBX, PNG, MP4, and log hashes match the captured baseline; JSON mesh counts are `160, 190, 170, 160, 190, 170`.

- [ ] **Step 6: Remove only temporary smoke artifacts**

Remove `/tmp/swim_fbx_demo-smoke.png`, `/tmp/swim_fbx_demo-smoke.mp4`, and the temporary checksum inventory after all comparisons complete.

Expected: no smoke artifact remains in the project or `/tmp`; all historical project outputs remain.
