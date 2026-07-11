# Benchmark Task 1 Report

## Status

Complete. Benchmark stages now resolve to an immutable `BenchmarkGraph` and
select real source, renderer, preview, and encoder components. No telemetry or
script changes were made.

## Commits

- `9ef7df8 perf: resolve benchmark stage graphs`
- `b9ab1c5 perf: add benchmark runtime lifecycle`
- `7eefe96 perf: execute real benchmark stage graphs`

## TDD RED evidence

1. Graph policy: `cmake --build build/macos --target swim_core_tests` failed
   because `swim/core/benchmark_stage.hpp` did not exist.
2. Decode-only lifecycle: the same target failed because `make_sources`,
   `stop_sources`, `run_decode_only`, and `DecodeOnlyExit` did not exist.
3. Resident/inactive lanes: the same target failed because `IRenderer` lacked
   `benchmark_frame` and `RenderCoordinator` lacked the resolved-graph
   constructor.
4. Backend graph contract: tests failed because `IBackend::make_renderer`
   still accepted only raw config. The full build then failed at the old app
   and Metal overrides and at Metal's missing `benchmark_frame`.

Each RED failed at the intended missing contract before implementation. Each
was followed by a GREEN run of `swim_core_tests`; the policy GREEN was committed
separately before runtime integration.

## Commands and results

- Baseline: `cmake --build build/macos --target swim_core_tests && ctest
  --test-dir build/macos --output-on-failure` — 10/10 tests passed.
- Policy/core cycles: `cmake --build build/macos --target swim_core_tests &&
  build/macos/swim_core_tests` — all registered core tests passed after each
  implementation step.
- Final build: `cmake --build build/macos` — passed with no compiler warnings.
- Final suite: `ctest --test-dir build/macos --output-on-failure` — 10/10 tests
  passed.
- Required smoke loop: every stage below ran with stream count 1 and duration
  1 second, exited zero, and emitted a final resolved graph record.

| Stage | Received / decoded | Render submissions | Preview presents | Encode completions | Resolved output |
|---|---:|---:|---:|---:|---|
| decode-only | 32 / 32 | 0 | 0 | 0 | none |
| render-only | 0 / 0 | 30 | 0 | 0 | none |
| decode-render | 33 / 33 | 30 | 0 | 0 | none |
| decode-render-preview | 33 / 33 | 29 | 25 | 0 | preview only |
| decode-render-encode | 32 / 32 | 30 | 0 | 30 | encode only |
| full | 32 / 32 | 30 | 0 | 0 | honored requested false/false |

The encode smoke reported `encode_using_hardware=true`, output dimensions
`5002x2102`, and `decoded_pixel_host_copies=0`. An additional
`--mode=benchmark --stage=decode-only` run exited zero after one second with
517 decoded/published frames and no renderer or output activity.

## Stage invariants

- Only the first `resolved_active_sources` are created or started. All source
  arrays, startup rollback, failure inspection, and cleanup tolerate null
  inactive entries.
- Decode-only constructs no renderer, preview, or encoder. The first decoded
  mailbox publication starts the shared lifecycle deadline; all-lane failure
  before activation requests stop and exits early.
- Render-only creates no source. The coordinator seeds all six fronts from six
  immutable `3840x2160` BGRA Metal textures.
- In every rendering stage, lanes at or above `active_sources` remain resident
  and their mailboxes are not consumed. Every accepted tick still reaches the
  existing six-mesh `5002x2102` Metal composition.
- Resident textures use `MTLStorageModePrivate` and are cleared once through a
  startup GPU render pass. There is no per-frame upload or resident-frame
  allocation.
- Realtime keeps the exact integer rational cadence. Benchmark mode loops as
  fast as fixed pools accept and performs a bounded 1 ms condition-variable
  backoff only on rejected submissions.
- The backend receives `BenchmarkGraph`; preview and encoder construction use
  resolved flags, not raw `AppConfig` booleans. Final metrics retain both the
  requested values and resolved graph.

## Concerns

- Six private resident 3840x2160 BGRA textures intentionally add about 190 MiB
  of persistent GPU memory per renderer.
- Forced `decode-render-encode` uses the existing config for encoder timing and
  sink details. The smoke covered the required fixed 30000/1001 hardware HEVC
  null sink; alternate invalid raw encode settings remain guarded by the
  encoder's existing constructor contracts.
