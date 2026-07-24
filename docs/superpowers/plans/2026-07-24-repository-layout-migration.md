# Repository Layout Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize the repository so source code, controlled inputs, build products, runtime outputs, tests, and documentation have distinct paths, while updating every consumer to the new layout and removing generated files from Git tracking.

**Architecture:** Keep production code under `cpp/` and `python/`; move annotation-preview Python tools into `python/annotation_preview/` and its Web tool beside them. Group controlled inputs under `inputs/{configs,pool,underwater}`. Keep runtime outputs under `outputs/`, build-generated runtime assets under ignored `build/assets/generated/`, and unify tests under `tests/`. All old paths are removed with no compatibility symlinks or wrappers.

**Tech Stack:** CMake 3.25+, C++20, Objective-C++/Metal, Python 3.10+, Bash, unittest/pytest-compatible Python tests, Git.

## Global Constraints

- Generated files leave Git tracking but remain on disk; do not delete, re-encode, or modify media/model contents.
- Direct path migration is required; do not preserve old directories, aliases, symlinks, or compatibility wrappers.
- `python/assets/` is production Python code and must not be confused with static input assets.
- Project-external 4K videos remain at `/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K` and are accessed through configuration or environment variables.
- Do not stage existing local FBX files, `.fbm/` texture directories, rendered images, videos, logs, JSON, CSV, or `.swasset` outputs.
- Every task ends with its focused verification and a separate commit containing only that task's files.

---

### Task 1: Make runtime configuration portable

**Files:**
- Modify: `cpp/core/src/config.cpp`
- Modify: `cpp/core/include/swim/core/config.hpp` only if the public declaration needs a helper or documented contract
- Modify: `cpp/tests/test_config.cpp`
- Modify: `configs/macos_20260629.conf` before it moves to `inputs/configs/macos_20260629.conf`
- Modify: `scripts/run_metal.sh`

**Interfaces:**
- Consumes: Existing `swim::core::load_config(const std::filesystem::path&)` and shell variable `SWIMMING_DATASET_DIR`.
- Produces: Config values containing `${VAR}` expand from the process environment; missing variables raise the existing path-and-line-number config error. `run_metal.sh` exports the dataset default before launching the executable.

- [ ] **Step 1: Add a failing C++ config test for environment expansion.**

  In `cpp/tests/test_config.cpp`, add a temporary config fixture containing:

  ```text
  source.cam3=${SWIMMING_DATASET_DIR}/cam3.mp4
  ```

  Set `SWIMMING_DATASET_DIR` to a temporary directory with `setenv`, load the fixture, and assert that `config.sources[0].path` equals the expanded path. Add a second assertion that `${MISSING_DATASET_DIR}` throws an exception whose message contains the config filename, line number, and variable name. Restore or unset both variables with an RAII guard so the test cannot leak process state.

- [ ] **Step 2: Run the focused config test before implementation.**

  Run:

  ```bash
  cmake -S . -B build/test-layout -DCMAKE_BUILD_TYPE=Debug
  cmake --build build/test-layout --target swim_tests -j2
  ctest --test-dir build/test-layout -R config --output-on-failure
  ```

  Expected: the new expansion assertion fails because the current parser treats `${...}` literally.

- [ ] **Step 3: Implement line-aware `${VAR}` expansion.**

  In `cpp/core/src/config.cpp`, add a private helper that scans a string for `${NAME}` tokens, calls `std::getenv`, substitutes the value, and calls `config_error(path, line_number, ...)` for an unterminated token, empty variable name, or missing environment variable. Apply it only to path-valued keys (`asset`, `source.cam*`, and `encode_path`); do not expand ordinary enum, boolean, or numeric values. Keep the existing duplicate-key and parse-error behavior unchanged.

- [ ] **Step 4: Export the dataset default in the Metal entrypoint.**

  In `scripts/run_metal.sh`, after computing `ROOT`, add:

  ```bash
  export SWIMMING_DATASET_DIR="${SWIMMING_DATASET_DIR:-/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K}"
  ```

  Do not hard-code the dataset path in the moved config file.

- [ ] **Step 5: Update the config fixture values.**

  Replace the six absolute `source.cam*` values in `configs/macos_20260629.conf` with `${SWIMMING_DATASET_DIR}/20260629_172532_camN.mp4`. Change `asset=assets/generated/pool_4k.swasset` to `asset=build/assets/generated/pool_4k.swasset`, and retain `encode_path=outputs/videos/pool_metal.h265`.

- [ ] **Step 6: Run the focused test and commit.**

  Run the commands from Step 2 again, then:

  ```bash
  git add cpp/core/src/config.cpp cpp/core/include/swim/core/config.hpp cpp/tests/test_config.cpp scripts/run_metal.sh configs/macos_20260629.conf
  git commit -m "feat(config): support portable runtime paths"
  ```

  Expected: the config test passes and the commit contains no generated files.

---

### Task 2: Move production tools and tests into stable code paths

**Files:**
- Move: `annotation-preview/common.py`, `detect_objects.py`, `export_object_frames.py`, `interpolate_a5f14.py`, `render_grid.py`, `render_preview.py` → `python/annotation_preview/`
- Move: `annotation-preview/dot_labeler/` → `python/annotation_preview/dot_labeler/`
- Move: `cpp/tests/*.cpp`, `cpp/tests/*.mm` → `tests/cpp/`
- Move: `cpp/tests/fixtures/` → `tests/fixtures/cpp/`
- Move: `python/tests/*.py` → `tests/python/`
- Move: `python/tests/fixtures/` → `tests/fixtures/python/`
- Modify: moved annotation-preview Python files, `CMakeLists.txt`, `README.md`, and any test imports discovered by `rg`

**Interfaces:**
- Consumes: Existing Python package imports and CMake source/fixture lists.
- Produces: `python.annotation_preview` importable package; CMake test targets use `tests/cpp` and `tests/fixtures/cpp`; Python test discovery uses `tests/python` and keeps all existing test names and assertions.

- [ ] **Step 1: Create destination package markers and move source files.**

  Run:

  ```bash
  mkdir -p python/annotation_preview tests/cpp tests/fixtures/cpp tests/python tests/fixtures/python
  touch python/annotation_preview/__init__.py
  git mv annotation-preview/common.py annotation-preview/detect_objects.py annotation-preview/export_object_frames.py annotation-preview/interpolate_a5f14.py annotation-preview/render_grid.py annotation-preview/render_preview.py python/annotation_preview/
  git mv annotation-preview/dot_labeler python/annotation_preview/dot_labeler
  git mv cpp/tests/*.cpp cpp/tests/*.mm tests/cpp/
  git mv cpp/tests/fixtures tests/fixtures/cpp
  git mv python/tests/*.py tests/python/
  git mv python/tests/fixtures tests/fixtures/python
  rmdir cpp/tests python/tests
  # Keep annotation-preview until Task 4 moves its generated detections.csv.
  ```

  Expected: all destination directories exist and the old source/test directories are absent.

- [ ] **Step 2: Update Python package imports and annotation tool defaults.**

  Change intra-tool imports to use `python.annotation_preview` or package-relative imports. Change generated CSV output from `annotation-preview/detections.csv` to `outputs/annotation_preview/detections.csv`. Keep external dataset paths configurable through command-line options or environment variables; do not replace one hard-coded project path with another.

- [ ] **Step 3: Update CMake test source and fixture paths.**

  Replace every `cpp/tests/...` source with `tests/cpp/...` and every `cpp/tests/fixtures/...` reference with `tests/fixtures/cpp/...` in `CMakeLists.txt` and CMake helper files. Preserve target names, compile definitions, and test commands.

- [ ] **Step 4: Update Python test imports and discovery commands.**

  Replace imports that rely on `python/tests` being a package with imports from the production `python` package. Update README and scripts from:

  ```bash
  .venv/bin/python -m unittest discover -s python/tests -v
  ```

  to:

  ```bash
  .venv/bin/python -m unittest discover -s tests/python -v
  ```

- [ ] **Step 5: Verify moved source and tests.**

  Run:

  ```bash
  .venv/bin/python -m unittest discover -s tests/python -v
  cmake -S . -B build/test-layout -DCMAKE_BUILD_TYPE=Debug
  cmake --build build/test-layout --target swim_tests -j2
  ctest --test-dir build/test-layout --output-on-failure
  ```

  Expected: Python and C++ tests pass with no import or fixture path errors.

- [ ] **Step 6: Commit the source/test move.**

  ```bash
  git add python/annotation_preview tests CMakeLists.txt README.md
  git commit -m "refactor: organize tools and tests by source path"
  ```

---

### Task 3: Reorganize controlled inputs by domain

**Files:**
- Move tracked: `inputs/models/pool.fbx` → `inputs/pool/models/pool.fbx`
- Move tracked: `inputs/textures/*.png` → `inputs/pool/textures/*.png`
- Move local untracked: `inputs/models/01d.fbx`, `1-5.fbx`, `1(2).fbx`, `all.fbx` and matching `.fbm/` directories → `inputs/underwater/models/`
- Modify: `python/assets/*.py`, `python/underwater/*.py`, `scripts/run_python.sh`, `README.md`, and tests that use input paths

**Interfaces:**
- Consumes: Existing CLI defaults and explicit `--tex-dir`/FBX arguments.
- Produces: Pool commands resolve `inputs/pool/models/pool.fbx` and `inputs/pool/textures`; underwater commands resolve `inputs/underwater/models` without changing extracted mesh format or ordering.

- [ ] **Step 1: Move pool inputs and local underwater inputs.**

  Run:

  ```bash
  mkdir -p inputs/pool/models inputs/pool/textures inputs/underwater/models
  git mv inputs/models/pool.fbx inputs/pool/models/pool.fbx
  git mv inputs/textures/*.png inputs/pool/textures/
  for item in \
    01d.fbx 01d.fbm \
    1-5.fbx 1-5.fbm \
    '1(2).fbx' '1(2).fbm' \
    all.fbx; do
    if [ -e "inputs/models/$item" ]; then
      mv "inputs/models/$item" inputs/underwater/models/
    fi
  done
  rmdir inputs/models inputs/textures
  ```

  Before running the local move, list the exact existing files and adjust only the listed FBX/`.fbm` inputs; never use a wildcard that could move unrelated files.

- [ ] **Step 2: Update pool Python defaults.**

  In `scripts/run_python.sh`, set the still/4K texture directory to `$ROOT/inputs/pool/textures`, the extraction default FBX to `$ROOT/inputs/pool/models/pool.fbx`, and all pool mesh JSON references to `$ROOT/outputs/data/pool_mesh.json`. Update `python/assets` defaults and help text consistently.

- [ ] **Step 3: Update underwater defaults and tests.**

  In `python/underwater/extract.py`, `python/underwater/render.py`, and `tests/python/test_underwater.py`, replace only the project-root input/output constants and fixtures. Keep `inputs/underwater/models` as an explicit caller-provided `--tex-dir`/FBX location so arbitrary underwater models remain supported.

- [ ] **Step 4: Verify input path behavior.**

  Run:

  ```bash
  .venv/bin/python -m unittest discover -s tests/python -v
  .venv/bin/python -m python.assets.extract_fbx inputs/pool/models/pool.fbx /tmp/pool-layout-mesh.json --tex-dir inputs/pool/textures
  .venv/bin/python -m python.underwater.extract inputs/underwater/models/1-5.fbx /tmp/underwater-layout-mesh.json --tex-dir inputs/underwater/models/1-5.fbm
  ```

  Expected: both commands exit 0 and write valid JSON with the same mesh/triangle counts as before.

- [ ] **Step 5: Commit the input move.**

  ```bash
  git add inputs python/assets python/underwater tests/python scripts/run_python.sh README.md
  git commit -m "refactor: organize pool and underwater inputs"
  ```

  Do not stage the local underwater FBX or `.fbm/` files unless they were already tracked.

---

### Task 4: Move generated assets and benchmark evidence out of source paths

**Files:**
- Move local: `assets/generated/` → `build/assets/generated/`
- Move local: `benchmarks/manual.jsonl`, `benchmarks/runs/`, and `benchmarks/latest` → `outputs/benchmarks/`
- Move local: `annotation-preview/detections.csv` if it still exists → `outputs/annotation_preview/detections.csv`
- Modify: `CMakeLists.txt`, `scripts/run_metal.sh`, `scripts/run_python.sh`, `python/assets/compile_runtime_asset.py`, `.gitignore`, and any benchmark reporting code
- Git index: remove tracked generated files under `outputs/`, `assets/generated/`, `benchmarks/`, and `.superpowers/` without deleting worktree files

**Interfaces:**
- Consumes: Mesh JSON at `outputs/data/pool_mesh.json` and benchmark command options.
- Produces: Runtime asset at `build/assets/generated/pool_4k.swasset`; benchmark JSONL and run links at `outputs/benchmarks/`; all generated files ignored.

- [ ] **Step 1: Update runtime asset paths in CMake and scripts.**

  Change `CMakeLists.txt` so `RUNTIME_ASSET` is `${CMAKE_SOURCE_DIR}/build/assets/generated/pool_4k.swasset`. Keep its custom command input `outputs/data/pool_mesh.json`, update `DEPENDS` only where paths changed, and retain the existing `runtime_asset` target. Update `scripts/run_python.sh`, `scripts/run_metal.sh`, and `inputs/configs/macos_20260629.conf` to use `build/assets/generated/pool_4k.swasset`.

- [ ] **Step 2: Move local benchmark and runtime files.**

  Run after listing existing files:

  ```bash
  mkdir -p build/assets/generated outputs/benchmarks outputs/annotation_preview
  if [ -f assets/generated/pool_4k.swasset ]; then mv assets/generated/pool_4k.swasset build/assets/generated/; fi
  if [ -f benchmarks/manual.jsonl ]; then mv benchmarks/manual.jsonl outputs/benchmarks/; fi
  if [ -d benchmarks/runs ]; then mv benchmarks/runs outputs/benchmarks/; fi
  if [ -L benchmarks/latest ]; then mv benchmarks/latest outputs/benchmarks/latest; fi
  if [ -f annotation-preview/detections.csv ]; then mv annotation-preview/detections.csv outputs/annotation_preview/; fi
  ```

  Remove only empty old directories after verifying their contents.

- [ ] **Step 3: Remove generated files from the Git index without deleting them.**

  Use `git rm --cached` only on tracked generated paths discovered by `git ls-files`, for example:

  ```bash
  git ls-files outputs assets/generated benchmarks .superpowers
  git rm -r --cached outputs assets/generated benchmarks .superpowers
  ```

  If a path is only a directory anchor such as `.gitkeep`, retain it only where the target directory needs an anchor; otherwise remove obsolete anchors with the directory.

- [ ] **Step 4: Replace ignore rules with the target policy.**

  In `.gitignore`, remove old `assets/generated/*` and `benchmarks/*` rules and add:

  ```gitignore
  build/
  outputs/
  .superpowers/
  .pytest_cache/
  inputs/underwater/models/
  inputs/pool/models/*.fbx
  ```

  Keep source, small controlled fixtures, and tracked pool textures visible to Git. Add `!outputs/.../.gitkeep` only if a command requires an empty directory anchor.

- [ ] **Step 5: Verify generated-file behavior.**

  Run:

  ```bash
  git ls-files outputs assets/generated benchmarks .superpowers
  git status --short --ignored
  cmake -S . -B build/test-layout -DCMAKE_BUILD_TYPE=Debug
  cmake --build build/test-layout --target runtime_asset -j2
  test -f build/assets/generated/pool_4k.swasset
  ```

  Expected: the first command prints no generated files, the moved local files are ignored, and the CMake target regenerates the asset in `build/assets/generated/`.

- [ ] **Step 6: Commit the generated-output migration.**

  ```bash
  git add .gitignore CMakeLists.txt scripts/run_metal.sh scripts/run_python.sh inputs/configs
  git commit -m "build: isolate generated assets and benchmark outputs"
  ```

  Do not stage ignored local outputs.

---

### Task 5: Finish annotation-preview runtime paths

**Files:**
- Modify: `python/annotation_preview/common.py`
- Modify: `python/annotation_preview/detect_objects.py`
- Modify: `python/annotation_preview/export_object_frames.py`
- Modify: `python/annotation_preview/interpolate_a5f14.py`
- Modify: `python/annotation_preview/render_grid.py`
- Modify: `python/annotation_preview/render_preview.py`
- Modify: `python/annotation_preview/dot_labeler/index.html`, `python/annotation_preview/dot_labeler/app.js` only if they reference moved CSV or image paths
- Modify: `README.md`, `docs/`, `.gitignore`

**Interfaces:**
- Consumes: External snapshot/object-frame data through explicit CLI arguments or environment variables.
- Produces: Preview images/HTML/CSV under `outputs/annotation_preview/`; source tools import as `python.annotation_preview.*`.

- [ ] **Step 1: Add a path smoke check for the moved package.**

  Run:

  ```bash
  .venv/bin/python -c 'import python.annotation_preview.common, python.annotation_preview.detect_objects, python.annotation_preview.render_grid'
  ```

  Expected before path fixes: this identifies any imports that still assume the old top-level directory; record each failure and fix only those import paths.

- [ ] **Step 2: Normalize annotation-preview output roots.**

  Add one repository-root-derived output constant or CLI default in the package and use it for generated CSV, grid renders, preview renders, and exported object frames. Do not hard-code `annotation-preview/` anywhere. Keep external data roots configurable and preserve current command-line behavior where possible.

- [ ] **Step 3: Verify with help and available local data.**

  Run:

  ```bash
  .venv/bin/python -m python.annotation_preview.detect_objects --help
  .venv/bin/python -m python.annotation_preview.render_grid --help
  .venv/bin/python -m python.annotation_preview.render_preview --help
  ```

  If the external snapshot dataset is unavailable, verify that help works and that a missing-data invocation fails with a clear path error rather than silently writing into the source tree.

- [ ] **Step 4: Commit annotation-preview path cleanup.**

  ```bash
  git add python/annotation_preview README.md docs .gitignore
  git commit -m "refactor: relocate annotation preview tooling"
  ```

---

### Task 6: Update documentation and remove every old path reference

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-10-project-layout-design.md` only if implementation details differ from the approved design
- Modify: any docs found by the old-path scan
- Modify: `.gitignore` if the scan finds stale exceptions

**Interfaces:**
- Consumes: New command paths from Tasks 1–5.
- Produces: A single documented layout and runnable command examples using only current paths.

- [ ] **Step 1: Update command examples.**

  Replace examples using `python/tests`, `inputs/models`, `inputs/textures`, `configs/`, `assets/generated`, `benchmarks/`, or top-level `annotation-preview` with the target paths. Keep existing commands functionally identical.

- [ ] **Step 2: Run an old-path scan.**

  Run:

  ```bash
  rg -n --hidden -g '!*.pyc' -g '!.git/**' -g '!.venv/**' -g '!build/**' -g '!outputs/**' -g '!inputs/**' '(annotation-preview/|assets/generated/|benchmarks/|configs/|inputs/models/|inputs/textures/|cpp/tests/|python/tests/)' .
  ```

  Expected: no matches outside historical changelog/spec text that explicitly documents a prior path. Update every operational match before continuing.

- [ ] **Step 3: Commit documentation and reference cleanup.**

  ```bash
  git add README.md docs .gitignore
  git commit -m "docs: document the organized repository layout"
  ```

---

### Task 7: Run full repository verification and finalize

**Files:**
- Modify only files required by verification failures; do not broaden the migration scope.

**Interfaces:**
- Consumes: All migrated source, input, output, test, build, and documentation paths.
- Produces: A clean, reproducible repository layout with passing build/test/runtime checks.

- [ ] **Step 1: Check the Git boundary.**

  Run:

  ```bash
  git diff --check
  git status --short
  git ls-files | rg '^(outputs/|assets/generated/|benchmarks/|\.superpowers/)' || true
  test ! -d annotation-preview
  test ! -d assets
  test ! -d benchmarks
  test ! -d configs
  ```

  Expected: no tracked generated files and no obsolete top-level directories; ignored local inputs/outputs may still exist on disk.

- [ ] **Step 2: Run Python verification.**

  Run:

  ```bash
  .venv/bin/python -m unittest discover -s tests/python -v
  .venv/bin/python -m python.assets.extract_fbx inputs/pool/models/pool.fbx /tmp/pool-layout-final.json --tex-dir inputs/pool/textures
  .venv/bin/python -m python.underwater.extract inputs/underwater/models/1-5.fbx /tmp/underwater-layout-final.json --tex-dir inputs/underwater/models/1-5.fbm
  ```

  Expected: all Python tests pass; both extraction commands write valid JSON.

- [ ] **Step 3: Run CMake and C++ verification.**

  Run:

  ```bash
  cmake -S . -B build/test-layout -DCMAKE_BUILD_TYPE=Debug
  cmake --build build/test-layout -j2
  ctest --test-dir build/test-layout --output-on-failure
  ```

  Expected: configure, build, runtime asset generation, and all C++ tests pass.

- [ ] **Step 4: Exercise user-facing shell entrypoints.**

  Run:

  ```bash
  ./scripts/run_python.sh --help
  ./scripts/run_metal.sh --help
  .venv/bin/python -m python.annotation_preview.detect_objects --help
  .venv/bin/python -m python.annotation_preview.render_grid --help
  ```

  Expected: every entrypoint resolves imports and new defaults without requiring an old path. If Metal hardware or the external dataset is unavailable, limit the check to `--help`/preflight and record that limitation.

- [ ] **Step 5: Run the old-path and artifact scans one final time.**

  Run:

  ```bash
  rg -n --hidden -g '!*.pyc' -g '!.git/**' -g '!.venv/**' -g '!build/**' -g '!outputs/**' -g '!inputs/**' '(annotation-preview/|assets/generated/|benchmarks/|configs/|inputs/models/|inputs/textures/|cpp/tests/|python/tests/)' .
  git ls-files | rg '^(outputs/|assets/generated/|benchmarks/|\.superpowers/)' || true
  git diff --check
  ```

  Expected: no operational old-path references, no generated files in Git, and no whitespace errors.

- [ ] **Step 6: Commit any verification-only fixes and report results.**

  If verification required a source fix, commit it with a focused message such as:

  ```bash
  git add <only-the-files-fixed>
  git commit -m "fix: correct migrated path after verification"
  ```

  Then report the final commit list, test commands, runtime checks, and any skipped hardware/external-data paths. Do not commit local ignored inputs or outputs.
