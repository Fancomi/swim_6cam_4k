# Metal Benchmark Matrix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce trustworthy paced and unpaced 1/2/4/6-camera measurements that isolate decode, render, preview, and hardware HEVC costs instead of relabeling one full pipeline.

**Architecture:** First make `BenchmarkStage` select a real runtime graph. Then add non-destructive concurrent metric sampling and one-line JSONL reporting. Finally drive a Release matrix from shell and summarize it with Python. Each layer is independently testable and reviewed before the next layer uses it.

**Tech Stack:** C++20, Objective-C++20, CMake/Ninja/CTest, Metal, VideoToolbox, libdispatch, macOS `proc_pid_rusage`, Python 3.10 standard library, POSIX shell, FFprobe only for offline validation.

## Global Constraints

- Six source files remain H.264 `3840x2160@30000/1001`; output encoding remains hardware-required HEVC at exact `5002x2102`.
- Camera order remains `cam3,cam2,cam1,cam4,cam5,cam6`; stream counts select the first N cameras in this exact order.
- Inactive render lanes use preallocated resident black frames; output geometry and six mesh draws do not shrink with stream count.
- `realtime` means paced `30000/1001`; `benchmark` means unpaced. JSON uses both `mode` and derived `pacing=\"paced\"|\"unpaced\"`.
- No production benchmark cell may enable CPU pixel readback, OpenCV, FFmpeg, software encode, per-frame application heap allocation, or an unbounded queue.
- Interval reporting never resets or races producer-owned state. Final reporting occurs after renderer/preview/encoder drain and remains exactly once on setup failures.
- Unknown measurements are errors during schema construction; they are never silently written as numeric zero.
- Debug runs are functional only. Publishable performance results require one Release binary SHA/build type across the whole run.
- A 15-second, 48-cell matrix is 720 seconds of measurement plus startup. Scripts support shorter functional smoke durations, but label them non-publishable.

---

### Task 1: Make Benchmark Stages Select Real Runtime Graphs

**Files:**
- Create: `cpp/core/include/swim/core/benchmark_stage.hpp`
- Create: `cpp/core/src/benchmark_stage.cpp`
- Create: `cpp/tests/test_benchmark_stage.cpp`
- Modify: `cpp/core/include/swim/core/backend.hpp`
- Modify: `cpp/core/include/swim/core/render_coordinator.hpp`
- Modify: `cpp/core/src/render_coordinator.cpp`
- Modify: `cpp/app/main.cpp`
- Modify: `cpp/backends/metal/src/metal_backend.mm`
- Modify: `cpp/tests/test_render_coordinator.cpp`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: `AppConfig::stage`, `mode`, `stream_count`, `preview`, and `encode`.
- Produces: `BenchmarkGraph resolve_benchmark_graph(const AppConfig&)`, real source/render/output activation, and fixed resident render-only frames.

- [ ] **Step 1: Write failing graph-policy tests**

```cpp
TEST_CASE(benchmark_stage_resolves_real_components) {
  const auto decode = resolve_benchmark_graph(config_for(decode_only, 2));
  CHECK_EQ(decode.active_sources, 2u);
  CHECK(!decode.create_renderer);
  CHECK(!decode.preview);
  CHECK(!decode.encode);

  const auto render = resolve_benchmark_graph(config_for(render_only, 4));
  CHECK_EQ(render.active_sources, 0u);
  CHECK(render.create_renderer);
  CHECK(render.synthetic_inputs);
  CHECK(!render.preview);
  CHECK(!render.encode);

  const auto encode =
      resolve_benchmark_graph(config_for(decode_render_encode, 6));
  CHECK_EQ(encode.active_sources, 6u);
  CHECK(encode.create_renderer);
  CHECK(!encode.preview);
  CHECK(encode.encode);
}
```

Also assert `full` alone honors the config preview/encode booleans,
`decode_render_preview` forces preview only, `decode_render_encode` forces
encode only, and `stream_count` outside `1,2,4,6` is rejected.

- [ ] **Step 2: Run RED**

Run: `cmake --build build/macos --target swim_core_tests`

Expected: FAIL because `BenchmarkGraph` and stage execution do not exist.

- [ ] **Step 3: Implement the graph policy**

```cpp
struct BenchmarkGraph final {
  std::uint32_t active_sources{};
  bool create_renderer{};
  bool synthetic_inputs{};
  bool preview{};
  bool encode{};
};

BenchmarkGraph resolve_benchmark_graph(const AppConfig& config);
std::string_view benchmark_stage_name(BenchmarkStage stage) noexcept;
std::string_view pacing_name(RunMode mode) noexcept;
```

The resolved graph, not raw config booleans, is passed to backend creation.
Do not mutate the user's `AppConfig`; report both requested and resolved values.

- [ ] **Step 4: Implement source-count and decode-only lifecycle**

`make_sources` creates only the first `active_sources`; source arrays and
cleanup tolerate null entries. Decode-only starts those sources, marks the run
active on the first decoded publication, measures for `duration`, and never
constructs a renderer, preview, or encoder. It exits early if every active
source fails before activation.

Add a core helper with a fake-source test so decode-only duration/stop behavior
does not depend on Apple frameworks.

- [ ] **Step 5: Implement resident render-only frames**

Extend `IRenderer` with:

```cpp
virtual FrameLease benchmark_frame(std::uint32_t camera_index) const = 0;
```

Metal allocates six immutable GPU-resident BGRA textures once at startup, each
`3840x2160`, filled black without a per-frame upload. `render_only` seeds all
six coordinator fronts from these frames and creates no source. For other
stages, lanes at indices `>= active_sources` use the same resident black frames.
Every render tick still executes the full six-mesh `5002x2102` composition.

- [ ] **Step 6: Make pacing authoritative**

`realtime` uses the existing exact rational cadence.
`benchmark` submits as fast as fixed resources accept and uses a bounded
condition-variable backoff on backpressure. Add coordinator tests proving the
stage graph does not wait for inactive mailboxes and unpaced mode is not
cadence-limited.

- [ ] **Step 7: Run graph integration tests**

```bash
cmake --build build/macos
ctest --test-dir build/macos --output-on-failure
for stage in decode-only render-only decode-render decode-render-preview decode-render-encode full; do
  build/macos/swim_realtime --config configs/macos_20260629.conf \
    --stage="$stage" --stream-count=1 --duration-seconds=1 \
    --preview=false --encode-sink=null --metrics="/tmp/stage-$stage.jsonl"
done
```

Expected: every stage exits zero; its final record contains the resolved graph;
decode-only reports no render submissions, render-only reports no received or
decoded frames, and output stages create only their named consumers.

- [ ] **Step 8: Commit**

```bash
git add CMakeLists.txt cpp/app cpp/core cpp/backends/metal cpp/tests
git commit -m "perf: execute real benchmark stage graphs"
```

---

### Task 2: Add Concurrent Interval Metrics and JSONL Reporting

**Files:**
- Create: `cpp/core/include/swim/core/benchmark_reporter.hpp`
- Create: `cpp/core/src/benchmark_reporter.cpp`
- Create: `cpp/core/include/swim/core/build_info.hpp.in`
- Create: `cpp/tests/test_benchmark_reporter.cpp`
- Modify: `cpp/core/include/swim/core/metrics.hpp`
- Modify: `cpp/core/src/metrics.cpp`
- Modify: `cpp/core/include/swim/core/backend.hpp`
- Modify: `cpp/core/src/render_coordinator.cpp`
- Modify: `cpp/backends/metal/src/metal_renderer.mm`
- Modify: `cpp/backends/metal/src/metal_preview.mm`
- Modify: `cpp/backends/metal/src/metal_encoder.mm`
- Modify: `cpp/backends/metal/src/videotoolbox_decoder.mm`
- Modify: `cpp/backends/metal/src/metal_backend.mm`
- Modify: `cpp/app/main.cpp`
- Modify: `cpp/tests/test_metrics.cpp`
- Modify: `cmake/AssertRuntimeFinalMetrics.cmake`
- Modify: `CMakeLists.txt`

**Interfaces:**
- Consumes: monotonic producer totals, fixed histograms, resolved graph, build and fingerprint manifest.
- Produces: one immutable interval sample per second plus one exactly-once final JSON line.

- [ ] **Step 1: Write RED tests for non-destructive concurrent samples**

Tests require two consecutive `sample_totals()` calls to leave counters intact,
interval delta calculation to be exact, p50/p95/p99 snapshots to be race-safe,
per-camera overwrite/reuse arrays to retain camera identity, and final snapshot
to include events already reported by interval samples.

Run: `cmake --build build/macos --target swim_core_tests`

Expected: FAIL because only destructive final snapshots exist.

- [ ] **Step 2: Implement monotonic totals and fixed concurrent histograms**

Keep producer counters cumulative. Add a non-destructive `MetricsTotals
sample_totals() const noexcept`. Reporter-owned previous totals produce interval
deltas. Replace render-thread-only histograms with fixed atomic bucket totals,
or an equivalent fixed double-buffer handoff, so reporter reads cannot race
observations. Record:

- per-camera receive/decode/publish/overwrite/reuse totals;
- frame-age p50/p95/p99 per camera;
- snapshot age-spread p99;
- GPU render duration p50/p95 from Metal command completion timestamps;
- render/preview/encode submissions, completions, drops, bytes, and FPS;
- per-camera decode surface/ticket capacity and high-water;
- render/output/encoder pool capacity, in-use, high-water, and misses;
- native wrapper creations and application hot-path allocation count.

No callback or render producer may allocate to record a sample.

- [ ] **Step 3: Add backend resource sampling**

```cpp
struct BackendRuntimeSample final {
  std::uint64_t gpu_allocated_bytes{};
};

virtual BackendRuntimeSample sample_runtime() const noexcept { return {}; }
```

Metal returns `MTLDevice.currentAllocatedSize`. The reporter obtains RSS with
`proc_pid_rusage` on macOS. These samples run on the reporter thread, never in
decode/render callbacks.

- [ ] **Step 4: Add build identity and fingerprint manifest**

CMake generates `build_info.hpp` containing the exact git SHA, build type, and
compiler identity. Add CLI `--benchmark-manifest=PATH`; the manifest is a
strict key/value file containing `run_id`, asset SHA-256, and six source
SHA-256 values produced by the matrix script. Missing/duplicate/invalid hashes
are fatal for benchmark mode, while normal realtime runs may omit the manifest.

- [ ] **Step 5: Write RED JSON reporter tests**

Assert JSON escaping, exact schema keys, six-element arrays, resolved graph,
paced/unpaced naming, build identity, hashes, machine fields, one newline, and
one `write()` call per record. A setup failure must still emit exactly one
`final:true` line with valid zero/empty-stage metrics.

- [ ] **Step 6: Implement the reporter**

`BenchmarkReporter` owns the output descriptor and one `std::jthread`. It wakes
once per second, samples totals/resources, derives deltas, constructs one JSON
string with explicit escaping, and performs one `write()` for the complete
line. The reporter stops before native drain, then a final sample is written
after renderer/preview/encoder drain so terminal completions are included.

Schema version 1 contains all fields in the original Task 12 schema plus:
`build_type`, `pacing`, `resolved_graph`, and explicit capacity/in-use values.
Schema validation rejects unavailable fields instead of manufacturing zeros.

- [ ] **Step 7: Run concurrency and runtime verification**

```bash
cmake --build build/macos
ctest --test-dir build/macos --output-on-failure
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --stage=decode-render-encode --stream-count=6 --encode-sink=null \
  --duration-seconds=3 --metrics=/tmp/interval.jsonl
```

Expected: at least two interval lines and exactly one final line; cumulative
counts never regress; final includes terminal encoder completions; zero
decoded-pixel host copies.

- [ ] **Step 8: Commit**

```bash
git add CMakeLists.txt cmake cpp/app cpp/core cpp/backends/metal cpp/tests
git commit -m "perf: add concurrent benchmark telemetry"
```

---

### Task 3: Build the Release Matrix Runner and Summaries

**Files:**
- Create: `python/validation/summarize_benchmarks.py`
- Create: `python/tests/test_summarize_benchmarks.py`
- Create: `scripts/run_metal_benchmarks.sh`
- Create: `scripts/run_metal_soak.sh`
- Create: `benchmarks/.gitkeep`
- Modify: `.gitignore`
- Modify: `README.md`

**Interfaces:**
- Consumes: one Release executable, six source paths, runtime JSONL schema 1.
- Produces: identity-safe per-cell JSONL, combined JSONL, CSV, and Markdown bottleneck summaries.

- [ ] **Step 1: Write failing summarizer tests**

Cover required-field validation, schema/build/run identity mismatch rejection,
duplicate/missing cells, nonzero host-copy rejection, grouping by
`(stage,stream_count,pacing)`, p50/p95 aggregation, and bottleneck ranking.

Run: `.venv/bin/python -m unittest discover -s python/tests -v`

Expected: FAIL because the summarizer is absent.

- [ ] **Step 2: Implement the standard-library summarizer**

```python
def load_records(path: Path) -> list[dict]: ...
def validate_matrix(records: list[dict], publishable: bool) -> None: ...
def summarize_records(records: list[dict]) -> list[SummaryRow]: ...
def write_csv(rows: list[SummaryRow], path: Path) -> None: ...
def write_markdown(rows: list[SummaryRow], path: Path) -> None: ...
```

Use only the Python standard library. Final rows drive the headline throughput;
interval rows provide stability/jitter. Report the lowest-throughput stage and
the incremental FPS/latency cost of preview and encode, not only full FPS.

- [ ] **Step 3: Implement the matrix runner**

The script:

1. configures/builds Release once and captures embedded SHA/build type;
2. computes and caches SHA-256 for the asset and six sources once;
3. creates one run ID and manifest;
4. executes six stages × four stream counts × paced/unpaced = 48 cells;
5. writes each cell to a unique file and retries no failed cell silently;
6. rejects mixed SHA/build type/schema and any production host copy;
7. combines only after all required cells pass;
8. invokes the summarizer for CSV/Markdown.

`--duration N` defaults to 15. Durations below 15 add
`publishable=false` to the manifest and report. `--quick` runs one second for
all cells as a functional smoke. Preview cells use a visible window only when
`--visible`; otherwise a benchmark offscreen present sink must still exercise
the preview GPU copy/present work without AppKit interaction.

- [ ] **Step 4: Implement soak runner**

The soak script runs the selected full six-stream paced cell for ten minutes by
default, samples RSS/GPU allocation slopes, and fails on host copies, pool
growth beyond configured capacity, fatal callback errors, or sustained FPS
below 29.0 after warm-up.

- [ ] **Step 5: Run quick functional matrix**

```bash
./scripts/run_metal_benchmarks.sh --quick
.venv/bin/python -m python.validation.summarize_benchmarks \
  benchmarks/latest/results.jsonl
```

Expected: all 48 cells exist once; identity is uniform; stage invariants hold;
zero production host copies; summaries identify throughput bottlenecks.

- [ ] **Step 6: Run publishable Release matrix**

```bash
./scripts/run_metal_benchmarks.sh --duration 15
```

Expected: 48 valid cells, 720 seconds of measured time, CSV/Markdown summaries,
and a recorded lowest-throughput stage for each stream count.

- [ ] **Step 7: Document and commit**

```bash
git add .gitignore README.md benchmarks/.gitkeep python/validation \
  python/tests scripts
git commit -m "perf: add trustworthy Metal benchmark matrix"
```

---

## Plan Self-Review

- Spec coverage: real stage graphs, stream counts, paced/unpaced execution,
  interval/final telemetry, exact identity, fixed resources, matrix runner,
  summaries, and soak gates each have an owning task.
- Placeholder scan: no `TBD`, `TODO`, or unspecified error-handling step.
- Type consistency: Task 1 produces `BenchmarkGraph`; Task 2 reports that graph;
  Task 3 validates the exact schema and graph invariants.
- The original seven-file Task 12 brief is superseded because static inspection
  proved it could only relabel the full pipeline and would race histograms.
