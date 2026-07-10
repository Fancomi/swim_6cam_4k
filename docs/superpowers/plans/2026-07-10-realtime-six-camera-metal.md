# Real-Time Six-Camera Metal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and measure a standalone C++/Metal application that reads six 4K H.264 MP4 files through AVFoundation and VideoToolbox, always renders each camera's latest decoded GPU surface into the existing 5002x2102 stitched view, and optionally previews or hardware-encodes the result without decoded-pixel host copies.

**Architecture:** A platform-neutral C++20 core owns configuration, fixed-capacity latest-frame mailboxes, health state, scheduling, and metrics. The macOS backend owns AVFoundation demux, VideoToolbox decode/encode, CoreVideo surface leases, Metal UV deformation/feather composition, and preview. Existing Python code becomes offline-only tooling for FBX extraction, runtime-asset compilation, golden rendering, and benchmark analysis.

**Tech Stack:** C++20, Objective-C++20, CMake 3.25+, Ninja, CTest, Apple Clang, AVFoundation, VideoToolbox, CoreVideo, Metal, MetalKit/Cocoa, Python 3.10, NumPy, OpenCV, FFprobe for offline verification only.

## Global Constraints

- Work only in this standalone repository; do not modify `sport-detect-haotian`.
- Initial machine: Apple M5, 10-core GPU, 16 GB unified memory, macOS 26.3.
- Initial inputs: six `3840x2160` H.264 MP4 files at `30000/1001` fps under `/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K`.
- Camera order is exactly `cam3`, `cam2`, `cam1`, `cam4`, `cam5`, `cam6`.
- Logical content is `5001x2101`; the GPU/encoder surface is `5002x2102` with one padded right column and bottom row.
- Production frame handling is C++/Objective-C++ only; Python never enters the per-frame runtime path.
- OpenCV and FFmpeg/FFprobe are offline validation tools only and must not link into `swim_realtime`.
- The runtime path uses latest-complete-frame semantics and never waits for matching camera timestamps.
- No decoded-pixel GPU-to-CPU-to-GPU copy, `glReadPixels`, BGR/RGB CPU conversion, or unbounded queue is allowed.
- After warm-up, application-owned access units, descriptors, in-flight records, and output surfaces come from fixed pools.
- Framework-required Metal/CoreVideo/VideoToolbox wrapper objects are permitted but must be measured separately.
- Runtime feathering matches the current normalized distance-transform weights and target R'G'B' blend semantics.
- Required local acceptance: six 4K streams and `5002x2102@30000/1001` preview for ten minutes, latest-frame-age p99 no more than two input periods, bounded memory/resources, and zero decoded-pixel host copies.
- The Ubuntu `egl-cuda` backend is a later plan; this plan defines its interface slot but does not implement it.
- Design source of truth: `docs/superpowers/specs/2026-07-10-realtime-six-camera-gpu-design.md`.

---

## Planned File Structure

```text
CMakeLists.txt
cmake/
  CompilerWarnings.cmake
cpp/
  app/
    main.cpp
  core/
    include/swim/core/
      asset.hpp
      asset_format.hpp
      backend.hpp
      camera_health.hpp
      config.hpp
      fixed_pool.hpp
      frame.hpp
      hot_path_allocations.hpp
      latest_frame_mailbox.hpp
      metrics.hpp
      render_coordinator.hpp
    src/
      asset.cpp
      backend.cpp
      camera_health.cpp
      config.cpp
      metrics.cpp
      render_coordinator.cpp
  backends/
    metal/
      include/swim/metal/
        metal_backend.hpp
        metal_encoder.hpp
        metal_frame.hpp
        metal_preview.hpp
        metal_renderer.hpp
        mp4_source.hpp
        videotoolbox_decoder.hpp
      src/
        metal_backend.mm
        metal_encoder.mm
        metal_preview.mm
        metal_renderer.mm
        mp4_source.mm
        videotoolbox_decoder.mm
      shaders/
        stitch.metal
  tests/
    test_asset.cpp
    test_camera_health.cpp
    test_config.cpp
    test_frame_mailbox.cpp
    test_main.cpp
    test_metrics.cpp
    test_render_coordinator.cpp
    test_support.hpp
    metal_decode_probe.mm
    metal_encoder_test.mm
    metal_golden_test.mm
    metal_preview_test.mm
python/
  __init__.py
  assets/
    __init__.py
    asset_format.py
    bake_uv.py
    compile_runtime_asset.py
    extract_fbx.py
    fbx_common.py
  validation/
    __init__.py
    compare_images.py
    reference_renderer.py
    summarize_benchmarks.py
  tests/
    __init__.py
    fixtures/tiny_mesh.json
    test_layout.py
    test_runtime_asset.py
assets/
  generated/.gitkeep
configs/
  macos_20260629.conf
benchmarks/
  .gitkeep
scripts/
  build_macos.sh
  compile_runtime_asset.sh
  run_metal_realtime.sh
  run_metal_benchmarks.sh
  run_metal_soak.sh
```

File responsibilities are intentionally narrow. `cpp/core` never includes an
Apple framework header. Every `.mm` file remains under the Metal backend.

---

### Task 1: Isolate Existing Python Offline Tools Without Changing Output

**Files:**
- Create: `python/__init__.py`
- Create: `python/assets/__init__.py`
- Create: `python/validation/__init__.py`
- Create: `python/tests/__init__.py`
- Create: `python/tests/test_layout.py`
- Move: `src/fbx_common.py` → `python/assets/fbx_common.py`
- Move: `src/bake_uv.py` → `python/assets/bake_uv.py`
- Move: `src/extract_fbx.py` → `python/assets/extract_fbx.py`
- Move: `src/render_pool.py` → `python/validation/reference_renderer.py`
- Modify: `scripts/run_4k.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: current Python CLI behavior and tracked golden images.
- Produces: module CLIs `python -m python.assets.{bake_uv,extract_fbx}` and `python -m python.validation.reference_renderer`; no top-level `src/` directory.

- [ ] **Step 1: Write the failing language-layout test**

```python
# python/tests/test_layout.py
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class LayoutTest(unittest.TestCase):
    def test_languages_are_isolated(self):
        self.assertFalse((ROOT / "src").exists())
        self.assertTrue((ROOT / "python/assets/extract_fbx.py").is_file())
        self.assertTrue((ROOT / "python/validation/reference_renderer.py").is_file())

    def test_reference_renderer_uses_repository_root(self):
        from python.validation import reference_renderer
        self.assertEqual(reference_renderer.PROJECT_ROOT, ROOT)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify the old layout fails**

Run: `.venv/bin/python -m unittest python.tests.test_layout -v`

Expected: FAIL because `src/` still exists and the new modules do not.

- [ ] **Step 3: Move the files and convert sibling imports to package imports**

Run:

```bash
mkdir -p python/assets python/validation python/tests
touch python/__init__.py python/assets/__init__.py python/validation/__init__.py python/tests/__init__.py
git mv src/fbx_common.py python/assets/fbx_common.py
git mv src/bake_uv.py python/assets/bake_uv.py
git mv src/extract_fbx.py python/assets/extract_fbx.py
git mv src/render_pool.py python/validation/reference_renderer.py
```

Use these exact import/root changes:

```python
# python/assets/bake_uv.py and python/assets/extract_fbx.py
from .fbx_common import find_texture_path, load_scene

# python/validation/reference_renderer.py
PROJECT_ROOT = Path(__file__).resolve().parents[2]
```

If the existing imported names differ, preserve those names and change only
the module prefix from `fbx_common` to `.fbx_common`.

- [ ] **Step 4: Align shell and README commands**

Change `scripts/run_4k.sh` to invoke:

```bash
"$PY" -m python.validation.reference_renderer \
  --data "$PROJECT_ROOT/outputs/data/pool_mesh.json" \
  --videos "${VIDEOS[@]}" \
  --video "$OUT" \
  --seconds "$SECONDS_ARG"
```

Replace every README `src/*.py` command with its `python -m` equivalent and
update the tree without changing the documented offline behavior.

- [ ] **Step 5: Run the layout and CLI smoke tests**

Run:

```bash
.venv/bin/python -m unittest python.tests.test_layout -v
.venv/bin/python -m python.validation.reference_renderer --help
.venv/bin/python -m python.assets.extract_fbx --help
.venv/bin/python -m python.assets.bake_uv --help
```

Expected: all unit tests PASS and all three CLIs exit 0.

- [ ] **Step 6: Verify the reference still remains unchanged**

Run:

```bash
cp outputs/images/pool.png /tmp/pool-before.png
.venv/bin/python -m python.validation.reference_renderer \
  --data outputs/data/pool_mesh.json \
  --tex-dir inputs/textures \
  --still /tmp/pool-after.png
shasum -a 256 /tmp/pool-before.png /tmp/pool-after.png
```

Expected: the two SHA-256 values are identical.

- [ ] **Step 7: Commit the isolated Python layout**

```bash
git add -A README.md scripts/run_4k.sh src python
git commit -m "refactor: isolate offline Python tools"
```

---

### Task 2: Define and Compile the Versioned Runtime Asset

**Files:**
- Create: `python/assets/asset_format.py`
- Create: `python/assets/compile_runtime_asset.py`
- Create: `python/tests/fixtures/tiny_mesh.json`
- Create: `python/tests/test_runtime_asset.py`
- Create: `scripts/compile_runtime_asset.sh`
- Create: `assets/generated/.gitkeep`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `outputs/data/pool_mesh.json`, `python.validation.reference_renderer.{to_meters,world_bounds,feather_weights}`.
- Produces: `python.assets.compile_runtime_asset.compile_asset(mesh_json: Path, output: Path, camera_ids: Sequence[str], ppm: float) -> None` and runtime format v1.

- [ ] **Step 1: Write a failing binary-format test**

Create a two-triangle, two-camera `tiny_mesh.json`, then write:

```python
# python/tests/test_runtime_asset.py
from pathlib import Path
import tempfile
import unittest

from python.assets.asset_format import HEADER, CAMERA, MAGIC, VERSION, read_header
from python.assets.compile_runtime_asset import compile_asset


FIXTURE = Path(__file__).parent / "fixtures/tiny_mesh.json"


class RuntimeAssetTest(unittest.TestCase):
    def test_compiler_writes_valid_offsets_and_camera_order(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "tiny.swasset"
            compile_asset(FIXTURE, output, ("cam3", "cam2"), ppm=10.0)
            header, cameras, body = read_header(output)
            self.assertEqual(header.magic, MAGIC)
            self.assertEqual(header.version, VERSION)
            self.assertEqual([c.camera_id for c in cameras], ["cam3", "cam2"])
            self.assertEqual(header.camera_record_bytes, CAMERA.size)
            self.assertGreater(len(body), 0)
```

- [ ] **Step 2: Run it and verify the missing compiler fails**

Run: `.venv/bin/python -m unittest python.tests.test_runtime_asset -v`

Expected: ERROR importing `python.assets.asset_format`.

- [ ] **Step 3: Implement the exact v1 binary structs**

```python
# python/assets/asset_format.py
import dataclasses
import struct
import zlib

MAGIC = b"SW4KAST\0"
VERSION = 1
HEADER = struct.Struct("<8s8I2Q32sI28s")       # 120 bytes
CAMERA = struct.Struct("<16s32s6I4Q16s")       # 120 bytes
VERTEX = struct.Struct("<4f")                   # output x/y, normalized u/v
INDEX = struct.Struct("<I")
WEIGHT = struct.Struct("<H")                    # R16_UNORM


@dataclasses.dataclass(frozen=True)
class Header:
    magic: bytes
    version: int
    header_bytes: int
    logical_width: int
    logical_height: int
    encoded_width: int
    encoded_height: int
    camera_count: int
    camera_record_bytes: int
    camera_table_offset: int
    body_bytes: int
    source_sha256: bytes
    body_crc32: int


@dataclasses.dataclass(frozen=True)
class CameraRecord:
    camera_id: str
    node_name: str
    vertex_count: int
    index_count: int
    weight_x: int
    weight_y: int
    weight_width: int
    weight_height: int
    vertices_offset: int
    indices_offset: int
    weights_offset: int
    weights_bytes: int
```

`read_header()` must reject a wrong magic/version/record size, decode all fixed
UTF-8 strings at the first NUL, bounds-check every blob, and verify
`zlib.crc32(file_bytes[HEADER.size:])` against `body_crc32`.

- [ ] **Step 4: Implement the compiler with cropped normalized weights**

The implementation must use this projection and quantization, which matches the
reference renderer:

```python
output_x = (position_x - xmin) * ppm
output_y = logical_height - 1 - (position_y - ymin) * ppm
weight_u16 = np.rint(np.clip(weight, 0.0, 1.0) * 65535.0).astype("<u2")
encoded_width = logical_width + (logical_width & 1)
encoded_height = logical_height + (logical_height & 1)
```

For each camera, deduplicate only identical `(output_x, output_y, u, v)` tuples,
write `VERTEX` and `uint32` index arrays, crop the full feather map to the
non-zero mask bounds, and write row-major R16 weights. Store absolute blob
offsets. Compute SHA-256 from the original mesh JSON bytes and CRC32 from every
byte after the 120-byte header. Reject camera-count mismatches and IDs longer
than 15 UTF-8 bytes.

The CLI is exact:

```text
python -m python.assets.compile_runtime_asset INPUT_JSON OUTPUT_SWASSET
    [--camera-ids cam3 cam2 cam1 cam4 cam5 cam6] [--ppm 100]
```

- [ ] **Step 5: Run the Python format tests**

Run: `.venv/bin/python -m unittest python.tests.test_runtime_asset -v`

Expected: PASS, including explicit corruption, truncated-file, wrong-order, and
camera-count tests.

- [ ] **Step 6: Generate and inspect the real runtime asset**

```bash
mkdir -p assets/generated
.venv/bin/python -m python.assets.compile_runtime_asset \
  outputs/data/pool_mesh.json assets/generated/pool_4k.swasset \
  --camera-ids cam3 cam2 cam1 cam4 cam5 cam6 --ppm 100
.venv/bin/python -c 'from pathlib import Path; from python.assets.asset_format import read_header; h,c,_=read_header(Path("assets/generated/pool_4k.swasset")); print(h.logical_width,h.logical_height,h.encoded_width,h.encoded_height,[x.camera_id for x in c])'
```

Expected: `5001 2101 5002 2102 ['cam3', 'cam2', 'cam1', 'cam4', 'cam5', 'cam6']`.

Add `assets/generated/*.swasset` to `.gitignore`; keep only `.gitkeep`. The
shell wrapper runs the command above relative to the repository root.

- [ ] **Step 7: Commit the asset compiler**

```bash
git add .gitignore assets/generated/.gitkeep python/assets python/tests scripts/compile_runtime_asset.sh
git commit -m "feat: compile versioned GPU runtime assets"
```

---

### Task 3: Add the C++20 Build, Test Harness, and Runtime Asset Loader

**Files:**
- Create: `CMakeLists.txt`
- Create: `cmake/CompilerWarnings.cmake`
- Create: `cpp/core/include/swim/core/asset_format.hpp`
- Create: `cpp/core/include/swim/core/asset.hpp`
- Create: `cpp/core/src/asset.cpp`
- Create: `cpp/tests/test_support.hpp`
- Create: `cpp/tests/test_main.cpp`
- Create: `cpp/tests/test_asset.cpp`
- Create: `scripts/build_macos.sh`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: runtime format v1 emitted by Task 2.
- Produces: `swim::core::RuntimeAsset load_asset(const std::filesystem::path&)` and CTest target `swim_core_tests`.

- [ ] **Step 1: Write the failing C++ asset-loader tests**

```cpp
// cpp/tests/test_asset.cpp
#include "test_support.hpp"
#include <swim/core/asset.hpp>

TEST_CASE(loads_real_asset_in_fixed_camera_order) {
  const auto asset = swim::core::load_asset(test_asset_path());
  CHECK_EQ(asset.logical_width, 5001u);
  CHECK_EQ(asset.logical_height, 2101u);
  CHECK_EQ(asset.encoded_width, 5002u);
  CHECK_EQ(asset.encoded_height, 2102u);
  CHECK_EQ(asset.cameras.size(), 6u);
  CHECK_EQ(asset.cameras[0].camera_id, "cam3");
  CHECK_EQ(asset.cameras[5].camera_id, "cam6");
}

TEST_CASE(rejects_corrupt_asset_crc) {
  CHECK_THROWS_WITH(swim::core::load_asset(corrupt_asset_path()),
                    "asset body CRC32 mismatch");
}
```

The test-support header must provide `TEST_CASE`, `CHECK`, `CHECK_EQ`, and
`CHECK_THROWS_WITH`, register functions in a static vector, and return a nonzero
process status when any case throws.

Use this minimal harness shape; `test_main.cpp` iterates `registry()`, prints
`PASS/FAIL <name>`, and returns the number of failures capped at 255:

```cpp
namespace swim::test {
using Function = void (*)();
struct Case { std::string_view name; Function function; };
inline std::vector<Case>& registry() { static std::vector<Case> r; return r; }
struct Register {
  Register(std::string_view name, Function f) { registry().push_back({name, f}); }
};
[[noreturn]] inline void fail(std::string_view expression,
                              std::source_location at = std::source_location::current()) {
  throw std::runtime_error(std::string(at.file_name()) + ":" +
                           std::to_string(at.line()) + ": " + std::string(expression));
}
template<class Function>
void check_throws_with(Function&& function, std::string_view expected) {
  try { function(); }
  catch (const std::exception& error) {
    if (std::string_view{error.what()} == expected) return;
    fail("exception message mismatch");
  }
  fail("expected exception was not thrown");
}
}
#define SWIM_JOIN2(a,b) a##b
#define SWIM_JOIN(a,b) SWIM_JOIN2(a,b)
#define TEST_CASE(name) \
  static void name(); \
  static ::swim::test::Register SWIM_JOIN(register_, __LINE__){#name, &name}; \
  static void name()
#define CHECK(expr) do { if (!(expr)) ::swim::test::fail(#expr); } while (false)
#define CHECK_EQ(a,b) CHECK((a) == (b))
#define CHECK_THROWS_WITH(expr,message) \
  ::swim::test::check_throws_with([&] { static_cast<void>(expr); }, message)
```

Configure `SWIM_TEST_ASSET_PATH` as an absolute compile definition pointing to
the generated real asset. Define these helpers in `test_asset.cpp`:

```cpp
std::filesystem::path test_asset_path() { return SWIM_TEST_ASSET_PATH; }
std::vector<std::byte> read_all_bytes(const std::filesystem::path& path) {
  std::ifstream input(path, std::ios::binary | std::ios::ate);
  if (!input) throw std::runtime_error("cannot read test asset");
  const auto size = static_cast<std::size_t>(input.tellg());
  std::vector<std::byte> bytes(size);
  input.seekg(0);
  input.read(reinterpret_cast<char*>(bytes.data()),
             static_cast<std::streamsize>(size));
  return bytes;
}
void write_all_bytes(const std::filesystem::path& path,
                     std::span<const std::byte> bytes) {
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  output.write(reinterpret_cast<const char*>(bytes.data()),
               static_cast<std::streamsize>(bytes.size()));
  if (!output) throw std::runtime_error("cannot write corrupt test asset");
}
std::filesystem::path corrupt_asset_path() {
  const auto path = std::filesystem::temp_directory_path() / "corrupt.swasset";
  auto bytes = read_all_bytes(test_asset_path());
  bytes.back() ^= std::byte{0x01};
  write_all_bytes(path, bytes);
  return path;
}
```

- [ ] **Step 2: Add CMake and verify the missing loader does not build**

Use CMake 3.25, `CMAKE_CXX_STANDARD 20`, no compiler extensions, and warnings
`-Wall -Wextra -Wpedantic -Wconversion -Wshadow`. Add a custom target
`runtime_asset` that runs Task 2's Python compiler before `swim_core_tests`.

```cmake
cmake_minimum_required(VERSION 3.25)
project(swim_realtime LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
find_package(Python3 3.10 REQUIRED COMPONENTS Interpreter)

set(RUNTIME_ASSET ${CMAKE_SOURCE_DIR}/assets/generated/pool_4k.swasset)
add_custom_command(
  OUTPUT ${RUNTIME_ASSET}
  COMMAND ${Python3_EXECUTABLE} -m python.assets.compile_runtime_asset
          ${CMAKE_SOURCE_DIR}/outputs/data/pool_mesh.json ${RUNTIME_ASSET}
          --camera-ids cam3 cam2 cam1 cam4 cam5 cam6 --ppm 100
  WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  DEPENDS outputs/data/pool_mesh.json
          python/assets/asset_format.py python/assets/compile_runtime_asset.py)
add_custom_target(runtime_asset DEPENDS ${RUNTIME_ASSET})

add_library(swim_core cpp/core/src/asset.cpp)
target_include_directories(swim_core PUBLIC cpp/core/include)
target_compile_options(swim_core PRIVATE -Wall -Wextra -Wpedantic -Wconversion -Wshadow)

enable_testing()
add_executable(swim_core_tests cpp/tests/test_main.cpp cpp/tests/test_asset.cpp)
target_link_libraries(swim_core_tests PRIVATE swim_core)
target_compile_definitions(swim_core_tests PRIVATE
  SWIM_TEST_ASSET_PATH="${RUNTIME_ASSET}")
add_dependencies(swim_core_tests runtime_asset)
add_test(NAME swim_core_tests COMMAND swim_core_tests)
```

Run:

```bash
cmake -S . -B build/macos -G Ninja -DCMAKE_BUILD_TYPE=Debug
cmake --build build/macos
```

Expected: compilation FAIL because `swim/core/asset.hpp` is absent.

Add `/build/` to `.gitignore` before configuring so Debug, Release, and
sanitizer trees never appear as source changes.

- [ ] **Step 3: Implement packed disk structs and owned runtime types**

```cpp
// cpp/core/include/swim/core/asset_format.hpp
#pragma once
#include <array>
#include <cstdint>

namespace swim::core::disk {
#pragma pack(push, 1)
struct AssetHeaderV1 {
  std::array<char, 8> magic;
  std::uint32_t version, header_bytes, logical_width, logical_height;
  std::uint32_t encoded_width, encoded_height, camera_count, camera_record_bytes;
  std::uint64_t camera_table_offset, body_bytes;
  std::array<std::uint8_t, 32> source_sha256;
  std::uint32_t body_crc32;
  std::array<std::uint8_t, 28> reserved;
};
struct CameraRecordV1 {
  std::array<char, 16> camera_id;
  std::array<char, 32> node_name;
  std::uint32_t vertex_count, index_count;
  std::uint32_t weight_x, weight_y, weight_width, weight_height;
  std::uint64_t vertices_offset, indices_offset, weights_offset, weights_bytes;
  std::array<std::uint8_t, 16> reserved;
};
struct VertexV1 { float output_x, output_y, u, v; };
#pragma pack(pop)
static_assert(sizeof(AssetHeaderV1) == 120);
static_assert(sizeof(CameraRecordV1) == 120);
static_assert(sizeof(VertexV1) == 16);
}
```

```cpp
// cpp/core/include/swim/core/asset.hpp
struct CameraAsset {
  std::string camera_id;
  std::string node_name;
  std::vector<disk::VertexV1> vertices;
  std::vector<std::uint32_t> indices;
  std::uint32_t weight_x, weight_y, weight_width, weight_height;
  std::vector<std::uint16_t> weights;
};
struct RuntimeAsset {
  std::uint32_t logical_width, logical_height, encoded_width, encoded_height;
  std::array<std::uint8_t, 32> source_sha256;
  std::vector<CameraAsset> cameras;
};
RuntimeAsset load_asset(const std::filesystem::path& path);
```

`asset.cpp` must read once at startup, validate every count/offset with
overflow-safe `offset <= file_size && size <= file_size - offset`, verify CRC32,
require six cameras for the production asset, and copy blobs into owned aligned
vectors. It must never reinterpret an unchecked offset.

- [ ] **Step 4: Build and run the focused loader tests**

Run:

```bash
cmake --build build/macos --target swim_core_tests
ctest --test-dir build/macos -R swim_core_tests --output-on-failure
```

Expected: all asset tests PASS.

- [ ] **Step 5: Commit the build and loader**

```bash
git add .gitignore CMakeLists.txt cmake cpp/core cpp/tests scripts/build_macos.sh
git commit -m "feat: load runtime assets in C++"
```

---

### Task 4: Implement Native Frame Leases and the Wait-Free Latest Mailbox

**Files:**
- Create: `cpp/core/include/swim/core/frame.hpp`
- Create: `cpp/core/include/swim/core/latest_frame_mailbox.hpp`
- Create: `cpp/tests/test_frame_mailbox.cpp`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: C++20 core test harness.
- Produces: `FrameLease`, `FrameMetadata`, and `LatestFrameMailbox::{publish,consume_latest}` used by all decode and render tasks.

- [ ] **Step 1: Write failing ownership and concurrency tests**

```cpp
TEST_CASE(mailbox_returns_latest_complete_generation) {
  LatestFrameMailbox box;
  MockNative native;
  box.publish(mock_frame(native, 1));
  box.publish(mock_frame(native, 2));
  box.publish(mock_frame(native, 3));
  FrameLease frame;
  CHECK(box.consume_latest(frame));
  CHECK_EQ(frame.metadata().sequence, 3u);
  CHECK(!box.consume_latest(frame));
}

TEST_CASE(inflight_copy_retains_native_surface) {
  MockNative native;
  { auto front = mock_frame(native, 7); auto inflight = front;
    CHECK_EQ(native.retains.load(), 2); }
  CHECK_EQ(native.releases.load(), 2);
}

TEST_CASE(mixed_rate_stress_never_regresses) {
  LatestFrameMailbox box;
  MockNative native;
  std::atomic_bool done{false};
  std::jthread producer([&] {
    for (std::uint64_t i = 1; i <= 2'000'000; ++i)
      box.publish(mock_frame(native, i));
    done.store(true, std::memory_order_release);
  });
  std::uint64_t previous = 0;
  FrameLease frame;
  while (!done.load(std::memory_order_acquire)) {
    if (box.consume_latest(frame)) {
      CHECK(frame.metadata().sequence > previous);
      previous = frame.metadata().sequence;
    }
  }
  while (box.consume_latest(frame)) {
    CHECK(frame.metadata().sequence > previous);
    previous = frame.metadata().sequence;
  }
  producer.join();
  CHECK_EQ(previous, 2'000'000u);
}
```

Define these helpers before the test cases in the same test file:

```cpp
struct MockNative {
  std::atomic_int retains{1};
  std::atomic_int releases{0};
};
void mock_retain(void* p) noexcept {
  static_cast<MockNative*>(p)->retains.fetch_add(1, std::memory_order_relaxed);
}
void mock_release(void* p) noexcept {
  static_cast<MockNative*>(p)->releases.fetch_add(1, std::memory_order_relaxed);
}
FrameLease mock_frame(MockNative& native, std::uint64_t sequence) {
  FrameMetadata m{};
  m.sequence = sequence;
  return FrameLease{&native, NativeLeaseOps{mock_retain, mock_release, 0x54455354}, m};
}
```

- [ ] **Step 2: Run and verify the missing types fail compilation**

Run: `cmake --build build/macos --target swim_core_tests`

Expected: FAIL with unknown `LatestFrameMailbox`/`FrameLease`.

- [ ] **Step 3: Implement the non-allocating lease value type**

```cpp
enum class PixelFormat : std::uint8_t { nv12_video_range, nv12_full_range, bgra8 };
enum class ColorMatrix : std::uint8_t { bt709, bt601, bt2020 };

struct FrameMetadata {
  std::uint32_t camera_index{}, width{}, height{};
  std::uint64_t sequence{}, decoder_generation{};
  std::int64_t pts_value{}, pts_timescale{};
  std::chrono::steady_clock::time_point arrived_at{}, decoded_at{};
  PixelFormat pixel_format{PixelFormat::nv12_video_range};
  ColorMatrix color_matrix{ColorMatrix::bt709};
  bool discontinuity{};
};

struct NativeLeaseOps {
  void (*retain)(void*) noexcept{};
  void (*release)(void*) noexcept{};
  std::uint32_t backend_tag{};
};

class FrameLease {
 public:
  FrameLease() = default;
  FrameLease(void* native, NativeLeaseOps ops, FrameMetadata metadata) noexcept;
  FrameLease(const FrameLease&);             // calls retain once
  FrameLease& operator=(const FrameLease&);  // retain new, release old
  FrameLease(FrameLease&&) noexcept;
  FrameLease& operator=(FrameLease&&) noexcept;
  ~FrameLease();                             // calls release once
  explicit operator bool() const noexcept;
  const FrameMetadata& metadata() const noexcept;
  void* native(std::uint32_t expected_backend_tag) const;
 private:
  void reset() noexcept;
  void* native_{};
  NativeLeaseOps ops_{};
  FrameMetadata metadata_{};
};
```

No constructor may allocate. `native()` throws on a backend-tag mismatch.

- [ ] **Step 4: Implement the three-slot atomic handoff**

```cpp
class LatestFrameMailbox {
  static constexpr std::uint8_t kDirty = 0x80;
  static constexpr std::uint8_t kIndexMask = 0x03;
  alignas(64) std::array<FrameLease, 3> slots_;
  alignas(64) std::atomic<std::uint8_t> middle_{1};
  std::uint8_t back_{2};          // producer-only
  alignas(64) std::uint8_t front_{0}; // consumer-only
 public:
  void publish(FrameLease frame) noexcept {
    slots_[back_] = std::move(frame);
    const auto previous = middle_.exchange(
        static_cast<std::uint8_t>(back_ | kDirty), std::memory_order_acq_rel);
    back_ = static_cast<std::uint8_t>(previous & kIndexMask);
  }
  bool consume_latest(FrameLease& output) {
    if ((middle_.load(std::memory_order_acquire) & kDirty) == 0) return false;
    const auto previous = middle_.exchange(front_, std::memory_order_acq_rel);
    if ((previous & kDirty) == 0) return false;
    front_ = static_cast<std::uint8_t>(previous & kIndexMask);
    output = slots_[front_];
    return true;
  }
};
```

Add static assertions that indices fit the flag encoding and document the
single-producer/single-consumer precondition.

- [ ] **Step 5: Run normal and sanitizer tests**

Run:

```bash
cmake --build build/macos --target swim_core_tests
ctest --test-dir build/macos -R swim_core_tests --output-on-failure
cmake -S . -B build/tsan -G Ninja -DCMAKE_BUILD_TYPE=Debug -DSWIM_SANITIZER=thread
cmake --build build/tsan --target swim_core_tests
ctest --test-dir build/tsan -R swim_core_tests --output-on-failure
```

Expected: all tests PASS and ThreadSanitizer reports zero races.

- [ ] **Step 6: Commit the mailbox**

```bash
git add CMakeLists.txt cpp/core/include/swim/core cpp/tests/test_frame_mailbox.cpp
git commit -m "feat: add latest-frame GPU lease mailbox"
```

---

### Task 5: Add Camera Health, Fixed Histograms, and Bounded Metrics

**Files:**
- Create: `cpp/core/include/swim/core/camera_health.hpp`
- Create: `cpp/core/src/camera_health.cpp`
- Create: `cpp/core/include/swim/core/metrics.hpp`
- Create: `cpp/core/src/metrics.cpp`
- Create: `cpp/core/include/swim/core/fixed_pool.hpp`
- Create: `cpp/core/include/swim/core/hot_path_allocations.hpp`
- Create: `cpp/core/src/hot_path_allocations.cpp`
- Create: `cpp/tests/test_camera_health.cpp`
- Create: `cpp/tests/test_metrics.cpp`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: frame timestamps and sequence numbers.
- Produces: `CameraHealthTracker`, `FixedLatencyHistogram`, `RuntimeCounters`, `HotPathAllocationScope`, and one-second immutable `MetricsSnapshot`.

- [ ] **Step 1: Write failing state and percentile tests**

```cpp
TEST_CASE(camera_becomes_stale_then_reconnecting) {
  CameraHealthTracker h{100ms, 1000ms};
  h.on_frame(t0);
  CHECK_EQ(h.tick(t0 + 99ms), CameraState::healthy);
  CHECK_EQ(h.tick(t0 + 100ms), CameraState::stale);
  CHECK_EQ(h.tick(t0 + 1000ms), CameraState::reconnecting);
}

TEST_CASE(histogram_reports_fixed_bucket_percentiles) {
  FixedLatencyHistogram h;
  for (int i = 1; i <= 100; ++i) h.observe(std::chrono::milliseconds{i});
  CHECK_EQ(h.percentile(0.50), 50ms);
  CHECK_EQ(h.percentile(0.95), 95ms);
  CHECK_EQ(h.percentile(0.99), 99ms);
}

TEST_CASE(global_new_is_counted_only_inside_hot_path_scope) {
  const auto before = hot_path_allocation_count();
  { HotPathAllocationScope scope; auto* p = new std::byte[8]; delete[] p; }
  CHECK_EQ(hot_path_allocation_count(), before + 1);
}

TEST_CASE(fixed_pool_never_grows_and_reuses_released_slot) {
  FixedSlotPool<std::uint64_t> pool{2};
  auto a = pool.try_acquire();
  auto b = pool.try_acquire();
  CHECK(a.has_value());
  CHECK(b.has_value());
  CHECK(!pool.try_acquire().has_value());
  const auto released_index = b->index();
  b.reset();
  auto c = pool.try_acquire();
  CHECK_EQ(c->index(), released_index);
  CHECK_EQ(pool.capacity(), 2u);
}
```

- [ ] **Step 2: Run and verify failure**

Run: `cmake --build build/macos --target swim_core_tests`

Expected: FAIL because health/metrics headers are absent.

- [ ] **Step 3: Implement explicit state transitions**

Use states `starting`, `healthy`, `stale`, `reconnecting`, and `failed`.
`on_frame()` moves any recoverable state to healthy. `tick()` uses the exact
100 ms and 1000 ms defaults. `on_unrecoverable_error()` is the only route to
failed. `next_reconnect_delay()` returns 250, 500, 1000, 2000, 4000, then 5000
ms and remains capped at 5000 ms.

- [ ] **Step 4: Implement allocation-free one-second metrics**

`FixedLatencyHistogram` uses 1001 `uint64_t` buckets for 0..1000 ms, clamps
larger values into the last bucket, and resets only after snapshot. Counters are
cache-line-separated atomics for received, decoded, published, overwritten,
reused, malformed, reconnects, render submissions, preview drops, encode drops,
decoded-pixel host copies, and pool exhaustion. JSON formatting occurs only on
the metrics/report thread from an immutable snapshot.

`FixedSlotPool<T>` allocates `1..64` stable slots only in its constructor. A
`uint64_t` atomic free-bit mask selects a slot with `std::countr_zero` and a CAS;
the move-only lease destructor returns it with `fetch_or`. Acquiring an empty
pool returns `std::nullopt`. Double release terminates in Debug builds. No pool
operation allocates or waits.

Override the executable's global C++ `operator new/new[]` to increment one
atomic only when the calling thread has an active `HotPathAllocationScope`.
Render/decode worker threads enter the scope after warm-up. Objective-C runtime
allocations are not counted here; increment explicit native counters when
creating `CVMetalTexture`, Metal command-buffer, VideoToolbox ticket, and output
callback wrapper objects.

- [ ] **Step 5: Run focused and full tests**

Run: `ctest --test-dir build/macos --output-on-failure`

Expected: all health, histogram, counter-reset, overflow-clamp, and reconnect
tests PASS.

- [ ] **Step 6: Commit health and metrics**

```bash
git add CMakeLists.txt cpp/core cpp/tests
git commit -m "feat: add bounded health and runtime metrics"
```

---

### Task 6: Add Configuration, Backend Registry, and a Testable CLI Shell

**Files:**
- Create: `cpp/core/include/swim/core/config.hpp`
- Create: `cpp/core/src/config.cpp`
- Create: `cpp/core/include/swim/core/backend.hpp`
- Create: `cpp/core/src/backend.cpp`
- Create: `cpp/app/main.cpp`
- Create: `cpp/tests/test_config.cpp`
- Create: `cpp/tests/fixtures/valid.conf`
- Create: `cpp/tests/fixtures/duplicate.conf`
- Create: `configs/macos_20260629.conf`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: six named source paths and runtime asset path.
- Produces: `AppConfig load_config(path)`, `BackendRegistry`, executable `swim_realtime`, and stable backend/source/renderer contracts.

- [ ] **Step 1: Write failing config validation tests**

```cpp
TEST_CASE(loads_exact_camera_order) {
  const auto c = load_config(fixture("valid.conf"));
  CHECK_EQ(c.backend, "metal");
  CHECK_EQ(c.sources[0].camera_id, "cam3");
  CHECK_EQ(c.sources[5].camera_id, "cam6");
  CHECK_EQ(c.fps_num, 30000u);
  CHECK_EQ(c.fps_den, 1001u);
}

TEST_CASE(rejects_duplicate_or_missing_camera) {
  CHECK_THROWS_WITH(load_config(fixture("duplicate.conf")),
                    "sources must be exactly cam3,cam2,cam1,cam4,cam5,cam6");
}
```

Configure `SWIM_TEST_FIXTURE_DIR` as an absolute test compile definition and
define `fixture(name)` as
`std::filesystem::path{SWIM_TEST_FIXTURE_DIR} / name`. `valid.conf` contains all
six required source keys in production order; `duplicate.conf` repeats
`source.cam3` and omits `source.cam6`.

- [ ] **Step 2: Run and verify failure**

Run: `cmake --build build/macos --target swim_core_tests`

Expected: FAIL because `config.hpp` is absent.

- [ ] **Step 3: Implement the dependency-free key/value config**

```cpp
enum class RunMode { realtime, benchmark };
enum class BenchmarkStage { full, decode_only, render_only, decode_render,
                            decode_render_preview, decode_render_encode };
struct SourceConfig { std::string camera_id; std::filesystem::path path; };
struct AppConfig {
  std::string backend{"metal"};
  RunMode mode{RunMode::realtime};
  BenchmarkStage stage{BenchmarkStage::full};
  std::filesystem::path asset_path;
  std::array<SourceConfig, 6> sources;
  std::uint32_t fps_num{30000}, fps_den{1001};
  bool preview{true}, encode{false};
  bool diagnostic_replacement{false};
  std::filesystem::path encode_path;
  std::chrono::milliseconds stale_after{100}, replace_after{1000};
  std::uint32_t decode_surface_pool{8}, decode_ticket_pool{16};
  std::uint32_t render_inflight{3}, output_pool{4};
  std::chrono::seconds duration{10};
  std::filesystem::path metrics_path;
};
AppConfig load_config(const std::filesystem::path&);
AppConfig apply_cli_overrides(AppConfig, std::span<const std::string_view>);
```

The parser accepts UTF-8 `key=value`, trims ASCII whitespace, ignores blank
lines and lines starting with `#`, rejects unknown/duplicate keys, and reports
`path:line: message`. Required source keys are `source.cam3`, `source.cam2`,
`source.cam1`, `source.cam4`, `source.cam5`, `source.cam6`.

`apply_cli_overrides` accepts only the flags used by this plan:
`--validate-only`, `--preview=true|false`, `--encode=true|false`,
`--diagnostic-replacement=true|false`,
`--encode-path=PATH`, `--encode-sink=file|null`, `--duration-seconds=N`,
`--mode=realtime|benchmark`, `--stage=NAME`, `--stream-count=1|2|4|6`, and
`--metrics=PATH`. Unknown or repeated flags fail before backend creation.

- [ ] **Step 4: Define narrow backend contracts**

```cpp
class ISource {
 public:
  virtual ~ISource() = default;
  virtual void start(LatestFrameMailbox& output) = 0;
  virtual void stop() noexcept = 0;
};
struct RenderSnapshot {
  std::array<FrameLease, 6> frames;
  std::chrono::steady_clock::time_point sampled_at;
};
class IRenderer {
 public:
  virtual ~IRenderer() = default;
  virtual bool submit(const RenderSnapshot&) = 0;
  virtual FrameLease replacement_frame(std::uint32_t camera_index) const = 0;
  virtual void drain() = 0;
};
class IBackend {
 public:
  virtual ~IBackend() = default;
  virtual std::unique_ptr<ISource> make_source(const SourceConfig&, std::uint32_t) = 0;
  virtual std::unique_ptr<IRenderer> make_renderer(const RuntimeAsset&, const AppConfig&) = 0;
  virtual void run_main_loop(std::stop_token) = 0;
  virtual void stop_main_loop() noexcept = 0;
};
using BackendFactory = std::unique_ptr<IBackend>(*)();
```

`BackendRegistry::create(name)` must fail with a sorted list of registered
names. Add a test-only `null` backend so the CLI can validate config and asset
without Apple code. The null backend's main loop waits on its private condition
variable until stop is requested. The Metal backend will run Cocoa on the main
thread while the coordinator runs on a worker thread.

- [ ] **Step 5: Add the real macOS configuration**

```ini
backend=metal
mode=realtime
stage=full
asset=assets/generated/pool_4k.swasset
source.cam3=/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K/20260629_172532_cam3.mp4
source.cam2=/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K/20260629_172532_cam2.mp4
source.cam1=/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K/20260629_172532_cam1.mp4
source.cam4=/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K/20260629_172532_cam4.mp4
source.cam5=/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K/20260629_172532_cam5.mp4
source.cam6=/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K/20260629_172532_cam6.mp4
fps_num=30000
fps_den=1001
preview=true
encode=false
diagnostic_replacement=false
encode_path=outputs/videos/pool_metal.h265
stale_ms=100
replace_ms=1000
decode_surface_pool=8
decode_ticket_pool=16
render_inflight=3
output_pool=4
duration_seconds=10
metrics=benchmarks/latest.jsonl
```

- [ ] **Step 6: Build and run CLI/config tests**

Run:

```bash
cmake --build build/macos
ctest --test-dir build/macos --output-on-failure
build/macos/swim_realtime --config configs/macos_20260629.conf --validate-only
```

Expected: tests PASS and validation prints the resolved six-camera order,
`5001x2101 -> 5002x2102`, backend `metal`, then exits 0.

- [ ] **Step 7: Commit the control shell**

```bash
git add CMakeLists.txt configs cpp/app cpp/core cpp/tests
git commit -m "feat: add runtime config and backend contracts"
```

---

### Task 7: Implement Metal Static-Texture Stitching and Golden Validation

**Files:**
- Create: `cpp/backends/metal/include/swim/metal/metal_frame.hpp`
- Create: `cpp/backends/metal/include/swim/metal/metal_renderer.hpp`
- Create: `cpp/backends/metal/src/metal_renderer.mm`
- Create: `cpp/backends/metal/shaders/stitch.metal`
- Create: `cpp/tests/metal_golden_test.mm`
- Create: `python/validation/compare_images.py`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: `RuntimeAsset`, six tracked PNG textures, `RenderSnapshot`.
- Produces: `MetalStitchRenderer`, diagnostic PNG output, and a measured GPU render result that matches the Python golden.

- [ ] **Step 1: Write the failing golden test command**

Add `metal_golden_test` that accepts:

```text
metal_golden_test ASSET OUTPUT.png TEXTURE_CAM3 TEXTURE_CAM2 TEXTURE_CAM1 TEXTURE_CAM4 TEXTURE_CAM5 TEXTURE_CAM6
```

It must load the six textures with ImageIO, render one frame, read back only
because this is a diagnostic executable, and write `OUTPUT.png`.

Run: `cmake --build build/macos --target metal_golden_test`

Expected: FAIL because the Metal renderer is absent.

- [ ] **Step 2: Add the backend-local Metal frame view**

```objective-c++
struct MetalContext {
  id<MTLDevice> device = nil;
  id<MTLCommandQueue> command_queue = nil;
  CVMetalTextureCacheRef texture_cache = nullptr;
};

struct MetalFrameView {
  id<MTLTexture> rgba = nil;  // golden path
  id<MTLTexture> luma = nil;  // production NV12 path
  id<MTLTexture> chroma = nil;
  swim::core::FrameMetadata metadata;
};

struct MetalOutputSlot {
  CVPixelBufferRef pixel_buffer = nullptr;
  CVMetalTextureRef texture_ref = nullptr;
  id<MTLTexture> texture = nil;
  std::atomic_uint32_t references{0};
  std::uint32_t pool_index = 0;
};

class MetalOutputLease {
 public:
  MetalOutputLease() = default;
  MetalOutputLease(const MetalOutputLease&) noexcept;
  MetalOutputLease& operator=(const MetalOutputLease&) noexcept;
  MetalOutputLease(MetalOutputLease&&) noexcept;
  MetalOutputLease& operator=(MetalOutputLease&&) noexcept;
  ~MetalOutputLease();
  CVPixelBufferRef pixel_buffer() const noexcept;
  id<MTLTexture> texture() const noexcept;
 private:
  friend class MetalOutputPool;
  explicit MetalOutputLease(MetalOutputSlot*) noexcept;
  MetalOutputSlot* slot_{};
};

class MetalOutputPool {
 public:
  MetalOutputPool(std::shared_ptr<MetalContext>, std::uint32_t capacity,
                  std::uint32_t width, std::uint32_t height);
  std::optional<MetalOutputLease> try_acquire() noexcept;
  std::uint32_t high_water() const noexcept;
 private:
  friend class MetalOutputLease;
  void release(MetalOutputSlot*) noexcept;
};

struct MetalRenderResult {
  MetalOutputLease output;
  std::uint64_t gpu_start_ns = 0;
  std::uint64_t gpu_end_ns = 0;
};
```

- [ ] **Step 3: Implement the shader contract**

The MSL file must define one shared vertex function, `stitch_rgba`,
`stitch_nv12`, and `resolve_accumulation`. The essential math is:

```metal
float weight = weight_texture.sample(linear_clamp, weight_uv).r;
float3 rgb = rgba_texture.sample(linear_mirror, in.uv).rgb;
return half4(half3(rgb * weight), half(weight));
```

For NV12, select BT.709 video/full-range coefficients from metadata and produce
target R'G'B' values before multiplying by weight. Configure additive blending
as source one plus destination one into `RGBA16Float`. Resolve into an
IOSurface-backed BGRA8 `CVPixelBuffer`, divide accumulated RGB by accumulated
weight when weight is nonzero, clamp to `[0,1]`, and force the padded
right column/bottom row to black.

- [ ] **Step 4: Implement static resource upload and fixed in-flight surfaces**

`MetalStitchRenderer` must:

- accept one shared `MetalContext`; the golden executable creates it directly,
  while Task 9's `MetalBackend` creates exactly one context and shares it with
  all six decoders and the renderer;
- compile `stitch.metal` at startup with `newLibraryWithSource` because this
  machine has Command Line Tools but no standalone `metal`/`metallib` command;
- upload six vertex/index buffers and six cropped R16_UNORM weights once;
- preallocate `config.render_inflight` accumulation textures and
  `config.output_pool` IOSurface-backed BGRA pixel buffers;
- return `false` instead of blocking when no in-flight/output slot is free;
- retain input/output leases until command-buffer completion;
- increment no decoded-pixel host-copy counter in production submission.

Enable CMake's `OBJCXX` language and link only the frameworks required here:
Metal, CoreVideo, CoreMedia, ImageIO, Foundation, and IOSurface. Task 10 adds
Cocoa/QuartzCore; Task 8 adds VideoToolbox/AVFoundation.

- [ ] **Step 5: Implement numeric comparison**

```python
# python/validation/compare_images.py
def compare(reference, candidate):
    a = cv2.imread(str(reference), cv2.IMREAD_COLOR).astype(np.float32)
    b = cv2.imread(str(candidate), cv2.IMREAD_COLOR).astype(np.float32)
    assert a.shape == b[:a.shape[0], :a.shape[1]].shape
    b = b[:a.shape[0], :a.shape[1]]
    mse = float(np.mean((a - b) ** 2))
    psnr = float("inf") if mse == 0 else 20 * np.log10(255) - 10 * np.log10(mse)
    ssim = compute_global_ssim(a, b)
    return {"psnr": psnr, "ssim": ssim}

def compute_global_ssim(a, b):
    c1, c2 = 6.5025, 58.5225
    mu_a, mu_b = float(a.mean()), float(b.mean())
    var_a, var_b = float(a.var()), float(b.var())
    covariance = float(((a - mu_a) * (b - mu_b)).mean())
    return ((2 * mu_a * mu_b + c1) * (2 * covariance + c2) /
            ((mu_a * mu_a + mu_b * mu_b + c1) *
             (var_a + var_b + c2)))
```

The CLI exits nonzero unless `PSNR >= 45` and `SSIM >= 0.995`, and writes an
amplified absolute-difference PNG beside the candidate.

- [ ] **Step 6: Run the full golden check**

```bash
cmake --build build/macos --target metal_golden_test
build/macos/metal_golden_test \
  assets/generated/pool_4k.swasset /tmp/pool-metal.png \
  inputs/textures/camera_3_composite.png \
  inputs/textures/camera_2_composite.png \
  inputs/textures/camera_1_composite.png \
  inputs/textures/camera_4_composite.png \
  inputs/textures/camera_5_composite.png \
  inputs/textures/camera_6_composite.png
.venv/bin/python -m python.validation.compare_images \
  outputs/images/pool.png /tmp/pool-metal.png
```

Expected: exit 0, `PSNR >= 45`, `SSIM >= 0.995`, and no shifted seam in the
difference image.

- [ ] **Step 7: Commit the static Metal renderer**

```bash
git add CMakeLists.txt cpp/backends/metal cpp/tests/metal_golden_test.mm python/validation
git commit -m "feat: render stitched assets with Metal"
```

---

### Task 8: Add Native MP4 Demux and VideoToolbox Decode Lanes

**Files:**
- Create: `cpp/backends/metal/include/swim/metal/mp4_source.hpp`
- Create: `cpp/backends/metal/src/mp4_source.mm`
- Create: `cpp/backends/metal/include/swim/metal/videotoolbox_decoder.hpp`
- Create: `cpp/backends/metal/src/videotoolbox_decoder.mm`
- Create: `cpp/tests/metal_decode_probe.mm`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: one H.264 MP4 `SourceConfig`, camera index, and `LatestFrameMailbox`.
- Produces: one independent `Mp4VideoToolboxSource` per camera and FrameLeases whose native handle is a pooled `MetalDecodedSurface` tagged `kMetalDecodedSurfaceTag`.

- [ ] **Step 1: Write the failing decode probe**

The probe opens one configured MP4, publishes 120 frames, consumes the latest
frames, and asserts:

```cpp
CHECK_EQ(frame.metadata().width, 3840u);
CHECK_EQ(frame.metadata().height, 2160u);
CHECK_EQ(frame.metadata().pixel_format, PixelFormat::nv12_video_range);
CHECK(frame.metadata().sequence > previous_sequence);
CHECK(decoder.using_hardware_acceleration());
CHECK_EQ(metrics.decoded_pixel_host_copies.load(), 0u);
```

Run: `cmake --build build/macos --target metal_decode_probe`

Expected: FAIL because the source/decoder classes are absent.

- [ ] **Step 2: Implement compressed MP4 reading**

`Mp4VideoToolboxSource` owns one worker thread, `AVURLAsset`, `AVAssetReader`,
and `AVAssetReaderTrackOutput` with `outputSettings:nil`, so it receives
compressed H.264 `CMSampleBufferRef` objects. It selects the first video track,
rejects non-H.264 formats, and passes samples plus PTS to its private decoder.
Realtime mode sleeps against the first valid PTS and a monotonic start time;
benchmark mode never sleeps.

Its constructor accepts the backend's shared `MetalContext`, so every decoded
surface uses the same `MTLDevice` and `CVMetalTextureCache` as the renderer.

- [ ] **Step 3: Implement explicit VideoToolbox decode**

Create `VTDecompressionSession` with destination attributes:

```objective-c++
@{
  (id)kCVPixelBufferPixelFormatTypeKey:
      @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
  (id)kCVPixelBufferMetalCompatibilityKey: @YES,
  (id)kCVPixelBufferIOSurfacePropertiesKey: @{}
}
```

Use a fixed `DecodeTicketPool`; each ticket stores camera index, display
sequence, decoder generation, PTS, arrival time, and mailbox pointer. Pass the
ticket through `sourceFrameRefCon`. In the decode callback:

1. reject nonzero status, dropped frames, wrong dimensions, late sequences, and
   callbacks from an old decoder generation;
2. read the pixel format and BT.709/range attachments;
3. acquire a `MetalDecodedSurface` slot, retain `imageBuffer`, create its luma
   and chroma texture wrappers, and build a `FrameLease` whose retain/release
   functions adjust the slot's intrusive reference count;
4. publish directly to the camera mailbox;
5. return the decode ticket to its fixed pool.

Query `kVTDecompressionPropertyKey_UsingHardwareAcceleratedVideoDecoder` after
session creation and fail production startup if it is false.

Define `MetalDecodedSurface` as a fixed-pool slot containing the retained
`CVPixelBufferRef`, one luma `CVMetalTextureRef`, one chroma
`CVMetalTextureRef`, their `id<MTLTexture>` views, and an intrusive atomic
reference count. Create the two texture wrappers once in the decode callback,
not on every render of a reused frame. The `FrameLease` retain/release callbacks
adjust that count; zero releases the CoreVideo objects and returns the slot to
the pool. A pool miss drops that decoded frame and increments pool exhaustion.

Recoverable reader/decoder errors increment the decoder generation and rebuild
only this lane using the 250 ms through 5 s backoff from `CameraHealthTracker`.
EOF before the configured run duration is a source failure. Late callbacks from
the old generation are released and never published.
When a new format description changes resolution or codec parameters, stop
submission to that decoder generation, drain its callbacks, rebuild only that
lane, and keep the last published front lease until the replacement lane
publishes or reaches the one-second replacement threshold.

- [ ] **Step 4: Run single-stream decode verification**

```bash
cmake --build build/macos --target metal_decode_probe
build/macos/metal_decode_probe \
  /Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K/20260629_172532_cam1.mp4 \
  --frames 120
```

Expected: exit 0; prints `3840x2160`, `30000/1001`, `hardware=true`, at least
120 callbacks, monotonic published sequence, and zero decoded-pixel host copies.

- [ ] **Step 5: Run six independent probes concurrently**

Start one lane for every configured source in one process for 10 seconds.
Expected: all lanes report frames; no camera adopts another camera's sequence or
native resource; ASan reports no leak/use-after-free.

- [ ] **Step 6: Commit native demux/decode**

```bash
git add CMakeLists.txt cpp/backends/metal cpp/tests/metal_decode_probe.mm
git commit -m "feat: decode native MP4 streams with VideoToolbox"
```

---

### Task 9: Integrate Six Latest-Frame Lanes with the Offscreen Render Loop

**Files:**
- Create: `cpp/core/include/swim/core/render_coordinator.hpp`
- Create: `cpp/core/src/render_coordinator.cpp`
- Create: `cpp/tests/test_render_coordinator.cpp`
- Create: `cpp/backends/metal/include/swim/metal/metal_backend.hpp`
- Create: `cpp/backends/metal/src/metal_backend.mm`
- Modify: `cpp/app/main.cpp`
- Modify: `cpp/backends/metal/src/metal_renderer.mm`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: six `ISource` instances, six mailboxes, one `IRenderer`, health/metrics, and `AppConfig`.
- Produces: `RenderCoordinator::run(stop_token)`, functioning `--backend metal`, and headless six-camera decode+render.

- [ ] **Step 1: Write failing coordinator behavior tests with fake sources**

```cpp
TEST_CASE(coordinator_reuses_stale_camera_without_waiting) {
  FakeRenderer renderer;
  CoordinatorFixture f{renderer};
  f.publish_all(1);
  f.tick(t0);
  f.publish(0, 2);  // only cam3 advances
  f.tick(t0 + 33ms);
  CHECK_EQ(renderer.snapshots[1].frames[0].metadata().sequence, 2u);
  for (int i = 1; i < 6; ++i)
    CHECK_EQ(renderer.snapshots[1].frames[i].metadata().sequence, 1u);
}
```

`FakeRenderer` implements `IRenderer`, appends each submitted snapshot to a
`std::vector<RenderSnapshot>`, returns true, returns a mock black lease from
`replacement_frame`, and makes `drain()` a no-op.
`CoordinatorFixture` owns six `MockNative` values, six mailboxes, a default
`AppConfig`, metrics, and `RenderCoordinator`; `publish(camera, sequence)` uses
the Task 4 `mock_frame` helper, and `publish_all(sequence)` calls it six times.
The test calls the public single-iteration method
`RenderCoordinator::tick(time_point)`; `run(stop_token)` is a deadline loop over
that same method.

- [ ] **Step 2: Run and verify failure**

Run: `cmake --build build/macos --target swim_core_tests`

Expected: FAIL because `RenderCoordinator` is absent.

- [ ] **Step 3: Implement the fixed-rate latest snapshot loop**

`RenderCoordinator` owns six current front leases. At every tick it calls
`consume_latest` once per mailbox, updates age/reuse/overwrite metrics, replaces
a missing camera with `renderer.replacement_frame(camera_index)` after one second,
and calls `renderer.submit(snapshot)` exactly once. It never uses a condition
variable shared by cameras. Realtime ticks use absolute `steady_clock` deadlines
at `fps_den/fps_num`; benchmark ticks immediately after the previous submit.

If the renderer returns false because its fixed pool is full, count a render
drop and continue. Do not spin waiting for a surface.

A Metal command-buffer error or device removal is fatal: record the native
error, request global stop, and exit nonzero after bounded in-flight cleanup.
Unsupported source codec/size fails only that source; invalid assets and an
unavailable selected backend fail before worker threads start.

- [ ] **Step 4: Register the Metal backend and wire lifecycle**

`main` performs this order:

```cpp
auto config = load_config(config_path);
auto asset = load_asset(config.asset_path);
auto backend = BackendRegistry::instance().create(config.backend);
auto renderer = backend->make_renderer(asset, config);
auto sources = make_six_sources(*backend, config);
start_sources(sources, mailboxes);
std::jthread render_thread([&](std::stop_token token) {
  RenderCoordinator coordinator{mailboxes, *renderer, config, metrics};
  coordinator.run(token);
  backend->stop_main_loop();
});
backend->run_main_loop(render_thread.get_stop_token());
render_thread.request_stop();
render_thread.join();
stop_sources(sources);
renderer->drain();
```

Handle SIGINT/SIGTERM with a signal-safe atomic flag polled by the main loop;
request stop from ordinary code, never from inside the signal handler. Always
write the final metrics line.

- [ ] **Step 5: Run a headless six-stream realtime smoke test**

```bash
cmake --build build/macos --target swim_realtime
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --preview=false --encode=false --duration-seconds=10
```

Expected: six sources healthy, output size `5002x2102`, render FPS at least
29.0 for the smoke run, no decoder waits, bounded in-flight counts, zero
decoded-pixel host copies, and clean shutdown.

- [ ] **Step 6: Commit integrated offscreen runtime**

```bash
git add CMakeLists.txt cpp/app cpp/core cpp/backends/metal
git commit -m "feat: render six latest VideoToolbox frames"
```

---

### Task 10: Add Non-Blocking Native Metal Preview

**Files:**
- Create: `cpp/backends/metal/include/swim/metal/metal_preview.hpp`
- Create: `cpp/backends/metal/src/metal_preview.mm`
- Create: `cpp/tests/metal_preview_test.mm`
- Modify: `cpp/backends/metal/src/metal_renderer.mm`
- Modify: `cpp/backends/metal/src/metal_backend.mm`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: completed IOSurface-backed BGRA output leases.
- Produces: `MetalPreview::offer(MetalOutputLease) -> bool`, one Cocoa window/CAMetalLayer, and preview drop/present metrics. `offer` returns false only when it replaced an unconsumed pending surface.

- [ ] **Step 1: Add a failing bounded-preview unit test around a fake presenter**

```cpp
TEST_CASE(preview_offer_never_blocks_when_capacity_one_is_full) {
  PreviewMailbox<std::uint64_t> p;
  CHECK(p.offer(1));
  const auto start = steady_clock::now();
  CHECK(!p.offer(2));
  CHECK(steady_clock::now() - start < 1ms);
  CHECK_EQ(p.drops(), 1u);
}
```

- [ ] **Step 2: Run and verify failure**

Run: `cmake --build build/macos --target metal_preview_test`

Expected: FAIL because preview mailbox/presenter is absent.

- [ ] **Step 3: Implement Cocoa/CAMetalLayer preview on the main thread**

`PreviewMailbox<Lease>` is a backend-local SPSC three-slot exchange using the
same dirty-bit/index protocol as
`LatestFrameMailbox`. All Metal completion callbacks first dispatch to one
serial completion queue, making them the single producer; the Cocoa main thread
is the single consumer. `offer` counts and replaces an unconsumed middle lease,
then returns false to report that replacement. Production instantiates
`PreviewMailbox<MetalOutputLease>`.

Create one `NSWindow` and one `CAMetalLayer` at startup. The render completion
handler offers only the newest completed output to a capacity-one preview
mailbox. The main-thread display callback consumes that lease, obtains the next
drawable, submits one GPU textured-quad/blit command, calls `presentDrawable`,
and releases the lease on completion. A nil drawable increments a drop counter
and returns immediately. No CPU readback or image conversion is allowed.

- [ ] **Step 4: Run the visible ten-second preview smoke test**

```bash
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --preview=true --encode=false --duration-seconds=10
```

Expected: a live stitched window, render loop remains near 29.97 fps, window
resize does not alter the 5002x2102 render surface, and closing the window
requests clean shutdown.

- [ ] **Step 5: Commit preview**

```bash
git add CMakeLists.txt cpp/backends/metal
git commit -m "feat: add non-blocking Metal preview"
```

---

### Task 11: Add Bounded VideoToolbox Hardware HEVC Encoding

**Files:**
- Create: `cpp/backends/metal/include/swim/metal/metal_encoder.hpp`
- Create: `cpp/backends/metal/src/metal_encoder.mm`
- Create: `cpp/tests/metal_encoder_test.mm`
- Modify: `cpp/backends/metal/src/metal_renderer.mm`
- Modify: `cpp/backends/metal/src/metal_backend.mm`
- Modify: `configs/macos_20260629.conf`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: completed BGRA `CVPixelBufferRef` output leases and monotonic frame PTS.
- Produces: `MetalEncoder::offer(MetalOutputLease, CMTime) -> bool`, Annex-B HEVC output, encode/drop/timing metrics.

The detailed contract and hardware capability evidence are in
`docs/superpowers/specs/2026-07-11-hardware-hevc-output-design.md`.

- [ ] **Step 1: Write a failing fixed-capacity encoder-input test**

```cpp
TEST_CASE(encoder_input_saturation_drops_without_blocking_renderer) {
  EncoderInputGate gate{2};
  auto first = gate.try_acquire();
  auto second = gate.try_acquire();
  CHECK(first.has_value());
  CHECK(second.has_value());
  CHECK(!gate.try_acquire().has_value());
  second.reset();
  CHECK(gate.try_acquire().has_value());
}
```

Also specify length-prefixed multi-NAL to Annex-B conversion, truncated and
zero-length rejection, VPS/SPS/PPS insertion on keyframes, non-contiguous block
input, and callback-owned output-lease lifetime. Use codec-neutral
`write_length_prefixed_nals_as_annex_b`; the payload is not an AVCC structure.

`EncoderInputGate` is a thin backend-local wrapper around
`FixedSlotPool<EncoderInputRecord>`. Each record contains one
`MetalOutputLease`, CMTime PTS, and submission sequence. `try_acquire()` returns
the pool's move-only lease; the VideoToolbox output callback resets the record
and releases the slot.

- [ ] **Step 2: Run and verify failure**

Run: `cmake --build build/macos --target metal_encoder_test`

Expected: FAIL because the encoder input abstraction is absent.

- [ ] **Step 3: Implement the real-time hardware encoder**

Create a hardware-required `VTCompressionSession` for exact `5002x2102`, HEVC,
with both require-hardware and enable-hardware encoder-specification keys. It
must fail startup if session preparation fails or
`UsingHardwareAcceleratedVideoEncoder` is not true; no software or resize
fallback is allowed.

Configure:

```objective-c++
VTCompressionSessionCreate(/* ... */, 5002, 2102, kCMVideoCodecType_HEVC,
                           encoder_specification, /* ... */);
VTSessionSetProperty(session, kVTCompressionPropertyKey_RealTime, kCFBooleanTrue);
VTSessionSetProperty(session, kVTCompressionPropertyKey_AllowFrameReordering, kCFBooleanFalse);
VTSessionSetProperty(session, kVTCompressionPropertyKey_ProfileLevel,
                     kVTProfileLevel_HEVC_Main_AutoLevel);
// Expected rate 30000/1001, average bitrate 60,000,000, keyframe interval 60.
```

Use a fixed input-record pool. `offer()` returns false immediately if no record
is available or `VTCompressionSessionEncodeFrame` rejects the frame. The output
callback converts HVCC length-prefixed NAL units to Annex-B start codes. Every
keyframe/IRAP emits VPS, SPS, and PPS before its coded slices. Conversion must
support non-contiguous `CMBlockBuffer` data with fixed scratch storage and reject
truncated or zero-length NAL units. It writes through one bounded writer owned
by the encoder. Compressed-byte I/O may run on the encoder callback path; it
must never make the render thread wait. `--encode-sink=null` discards encoded
bytes after counting them for pure encoder benchmarks. Frame PTS is exactly
`sequence * 1001 / 30000` seconds.

Fan out completed renderer leases to preview and encoder without copying the
pixels. The encoder gate starts at capacity two and publishes submissions,
completions, bytes, drops/errors, completion FPS, occupancy/high-water, misses,
and bounded-drain status. Shutdown stops admission, drains renderer completion,
flushes the serial completion router, closes preview admission, completes all
VideoToolbox frames, drains the callback gate, closes the writer, and only then
invalidates the session. Callback state must remain alive after a drain timeout.

- [ ] **Step 4: Encode and validate a five-second sample**

```bash
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --preview=false --encode=true \
  --encode-path=outputs/videos/pool_metal_5s.h265 --duration-seconds=5
ffprobe -v error -f hevc -select_streams v:0 \
  -show_entries stream=codec_name,width,height,r_frame_rate \
  -of default=noprint_wrappers=1 outputs/videos/pool_metal_5s.h265
```

Expected: `codec_name=hevc`, `width=5002`, `height=2102`, decodable stream,
monotonic PTS, no decoded-pixel host copies, and no render-thread wait.

- [ ] **Step 5: Run preview and encode together**

Run for 30 seconds with both enabled. Expected: fixed output pool remains within
its configured capacity; slow output causes counted preview/encode drops, not
mailbox growth or render blocking.

- [ ] **Step 6: Commit encoding**

```bash
git add CMakeLists.txt configs cpp/backends/metal
git commit -m "feat: add bounded VideoToolbox HEVC output"
```

---

### Task 12: Implement the Benchmark Matrix and Machine-Readable Reports

**Files:**
- Create: `python/validation/summarize_benchmarks.py`
- Create: `scripts/run_metal_benchmarks.sh`
- Create: `scripts/run_metal_soak.sh`
- Create: `benchmarks/.gitkeep`
- Modify: `cpp/core/src/metrics.cpp`
- Modify: `cpp/app/main.cpp`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: benchmark stage, stream count 1/2/4/6, real-time/unpaced mode, runtime counters.
- Produces: append-only JSONL records and Markdown/CSV summaries for every required performance cell.

- [ ] **Step 1: Write a failing report-schema test**

```python
def test_summary_rejects_missing_copy_and_age_metrics(self):
    with self.assertRaisesRegex(ValueError, "decoded_pixel_host_copies"):
        summarize_record({"render_fps": 30.0})

def test_summary_groups_stage_and_stream_count(self):
    rows = summarize_records(valid_records())
    self.assertIn(("decode_render", 6, "unpaced"), rows)
```

- [ ] **Step 2: Run and verify failure**

Run: `.venv/bin/python -m unittest discover -s python/tests -v`

Expected: FAIL because the summarizer is absent.

- [ ] **Step 3: Finalize the JSONL schema**

Every one-second and final record must include:

```json
{
  "schema": 1,
  "run_id": "UTC timestamp plus short git SHA",
  "final": false,
  "backend": "metal",
  "mode": "realtime",
  "stage": "decode_render_preview",
  "stream_count": 6,
  "elapsed_s": 1.0,
  "render_fps": 29.97,
  "preview_fps": 29.97,
  "encode_fps": 0.0,
  "gpu_render_ms_p50": 0.0,
  "gpu_render_ms_p95": 0.0,
  "frame_age_ms_p50": [0, 0, 0, 0, 0, 0],
  "frame_age_ms_p95": [0, 0, 0, 0, 0, 0],
  "frame_age_ms_p99": [0, 0, 0, 0, 0, 0],
  "snapshot_age_spread_ms_p99": 0.0,
  "mailbox_overwrites": [0, 0, 0, 0, 0, 0],
  "frame_reuses": [0, 0, 0, 0, 0, 0],
  "render_drops": 0,
  "preview_drops": 0,
  "encode_drops": 0,
  "decoded_pixel_host_copies": 0,
  "decode_surface_pool_high_water": [0, 0, 0, 0, 0, 0],
  "decode_ticket_pool_high_water": [0, 0, 0, 0, 0, 0],
  "render_inflight_high_water": 0,
  "output_pool_high_water": 0,
  "rss_bytes": 0,
  "gpu_allocated_bytes": 0,
  "application_owned_frame_allocations": 0,
  "native_wrapper_creations": {
    "cv_metal_texture": 0,
    "metal_command_buffer": 0,
    "videotoolbox_ticket": 0
  },
  "asset_sha256": "hex",
  "source_sha256": ["hex", "hex", "hex", "hex", "hex", "hex"],
  "git_sha": "hex",
  "machine": {},
  "resolved_config": {}
}
```

Write JSON manually from immutable snapshots on the report thread; escape all
strings and write one complete line per `write()` call.

- [ ] **Step 4: Implement the exact benchmark matrix**

`run_metal_benchmarks.sh` executes 15-second unpaced and paced runs for stages:

```text
decode_only
render_only
decode_render
decode_render_preview
decode_render_encode
full
```

for stream counts `1 2 4 6`. It records the exact build SHA and refuses to mix
Debug and Release results. `render_only` creates six resident synthetic textures
once and performs no per-frame upload. `decode_only` never creates a renderer.
Stream counts select the first N cameras in asset order; inactive camera regions
use the preallocated black replacement, so dimensions and shader work remain
explicit in each stage rather than silently changing the output geometry.
The script computes and caches SHA-256 for each source file before starting the
matrix; the timed child process reads the cached fingerprints instead of
hashing 27 GB during startup.

- [ ] **Step 5: Run the short matrix and generate summaries**

```bash
cmake -S . -B build/release -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/release
./scripts/run_metal_benchmarks.sh --duration 15
.venv/bin/python -m python.validation.summarize_benchmarks benchmarks/latest/results.jsonl
```

Expected: every stage/stream/mode cell appears once, all production cells report
zero decoded-pixel host copies, and the summary identifies the lowest-throughput
stage instead of reporting only end-to-end FPS.

- [ ] **Step 6: Commit benchmark tooling**

```bash
git add .gitignore benchmarks/.gitkeep cpp/core/src/metrics.cpp cpp/app/main.cpp python/validation scripts
git commit -m "perf: add six-stream benchmark matrix"
```

---

### Task 13: Run Correctness, Sanitizers, Ten-Minute Soak, and Document Results

**Files:**
- Modify: `README.md`
- Create: `docs/benchmarks/m5-metal-baseline.md`
- Modify: `scripts/run_metal_soak.sh`

**Interfaces:**
- Consumes: all previous tasks and acceptance criteria.
- Produces: reproducible build/run documentation, verified M5 performance report, and a clean handoff point for the later Ubuntu plan.

- [ ] **Step 1: Run all Python and C++ correctness tests from a clean build**

```bash
rm -rf build/verify
.venv/bin/python -m unittest discover -s python/tests -v
cmake -S . -B build/verify -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build/verify
ctest --test-dir build/verify --output-on-failure
```

Expected: zero failed Python or C++ tests.

- [ ] **Step 2: Run address/undefined and thread sanitizer builds**

```bash
cmake -S . -B build/asan -G Ninja -DCMAKE_BUILD_TYPE=Debug -DSWIM_SANITIZER=address,undefined
cmake --build build/asan
ctest --test-dir build/asan --output-on-failure
cmake -S . -B build/tsan -G Ninja -DCMAKE_BUILD_TYPE=Debug -DSWIM_SANITIZER=thread
cmake --build build/tsan
ctest --test-dir build/tsan --output-on-failure
```

Expected: zero sanitizer findings. Metal/VideoToolbox integration tests that a
sanitizer cannot execute must be listed explicitly, not silently skipped.

- [ ] **Step 3: Re-run the image golden**

Run Task 7's exact golden command against the Release build.

Expected: `PSNR >= 45`, `SSIM >= 0.995`, correct camera order, and no seam shift.

- [ ] **Step 4: Run the full ten-minute acceptance soak**

```bash
./scripts/run_metal_soak.sh --duration 600 \
  --config configs/macos_20260629.conf
```

The script fails unless all of these are true:

```text
six healthy 3840x2160 inputs for the full run
5002x2102 render/preview >= 29.90 fps average
each camera frame-age p99 <= 66.734 ms
decoded_pixel_host_copies == 0
application_owned_frame_allocations == 0 after warm-up
render/output pool high-water <= configured capacity
no sustained RSS or GPU-memory increase after the first 120 seconds
no partial, corrupt, or cross-camera frame
clean shutdown with every native lease released
```

Define “no sustained increase” mechanically: compare the median of seconds
120–179 with the median of the last 60 seconds. RSS and
`MTLDevice.currentAllocatedSize` may each rise by at most 32 MiB, and every
fixed-pool live count at shutdown must equal its pre-run count.

Run a second ten-minute soak with encode enabled. Encoding below 29.90 fps is
not hidden: record the limiting stage and keep the preview acceptance result
separate.

- [ ] **Step 5: Write the evidence-based M5 baseline**

`docs/benchmarks/m5-metal-baseline.md` must contain the exact machine/OS/build,
git SHA, asset checksum, matrix table, golden metrics, ten-minute percentiles,
pool high-water marks, memory graph/table, encoder result, observed bottleneck,
and the exact reproduction commands. Do not estimate missing numbers.

- [ ] **Step 6: Rewrite README around the two clearly separated workflows**

Document:

1. offline Python FBX/reference workflow;
2. C++ Metal build, asset compilation, validation, realtime replay, benchmark,
   preview, and encode workflow;
3. backend interface and explicit statement that `egl-cuda` is not implemented
   yet;
4. no OpenCV/FFmpeg in production hot path;
5. dataset override without embedding data in the repository.

- [ ] **Step 7: Run final documentation commands and verify a clean tree**

```bash
./scripts/build_macos.sh
./scripts/compile_runtime_asset.sh
build/release/swim_realtime --config configs/macos_20260629.conf --validate-only
git diff --check
git status --short
```

Expected: build/asset/config commands exit 0; `git diff --check` is empty; only
the intended README/report/script changes are uncommitted before the final
commit.

- [ ] **Step 8: Commit the verified Metal milestone**

```bash
git add README.md docs/benchmarks/m5-metal-baseline.md scripts/run_metal_soak.sh
git commit -m "docs: record verified M5 Metal baseline"
```

After this commit, create a new Ubuntu-specific design/plan using the measured
interfaces and target NVIDIA GPU/driver details. Do not implement Ubuntu in this
plan.
