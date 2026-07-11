# Benchmark Task 2 Review Fix Report

## Status

Complete. The Task 2 review blockers C1, C2, and I1 are fixed and verified.

Review baseline: `cba7c8e`

Fix commits:

- `fcb9786` `fix: detach native callback telemetry safely`
- `1bd14b4` `fix: harden benchmark telemetry finalization`

The shared branch also received the independent Task 3 commits while this fix
was in progress. No Task 3 file or hunk was included in either fix commit.

## RED Evidence

1. The first core RED failed to compile because
   `RuntimeCounterPublication` did not exist and RSS/GPU resources could not be
   represented as `std::nullopt`.
2. The encoder timeout regression deterministically claimed a callback, blocked
   it inside the writer, let `close_and_drain()` time out, then released the
   callback. The existing implementation changed external `encode_bytes` after
   timeout, failing
   `timeout_detaches_external_metrics_before_blocked_callback_returns`.
3. Reporter RED covered a background partial write. The old interval thread
   silently caught the error, `stop_intervals()` did not propagate it, and the
   final path performed another write.
4. Additional reporter REDs required an actual partial `write(2)` to be rolled
   back to an empty file, a negative final write to be attempted once, explicit
   backend unbinding, unsupported resources to serialize as `null`, and a
   genuinely measured zero to remain numeric `0`.

## C1: Native Callback and Lease Lifetime

`RuntimeCounterPublication` is a startup-owned bridge with two operations:

- `publish()` serializes only short atomic counter updates against terminal
  detachment.
- `finalize()` performs the final gauge publication exactly once, clears the
  external pointer while holding the same mutex, and waits for any already
  entered atomic publication to leave. After it returns, late callbacks and
  retained leases update only self-owned atomics.

The bridge is now used by all four review sites:

| Site | Self-owned state and detach point |
|---|---|
| Encoder | callback-retained `State`; final detach in encoder metrics flush, including timeout paths |
| Renderer | callback-retained `Impl`; final detach after bounded completion-gate drain or timeout |
| Preview | callback-retained `Impl`; final detach after presentation drain or timeout |
| Decoder | decoder and retained surface pool share the bridge; decoder destruction detaches it before a surface lease can outlive runtime counters |

No publication mutex covers a writer call, native framework call, condition
wait, or drain wait. Only fixed atomic loads/stores/adds execute under it.
Six 29.97 fps decoder lanes produce about 180 callbacks/s. Even the successful
decode path's several uncontended, short atomic publication sections are small
relative to six 3840x2160 hardware decodes and the 5002x2102 GPU composition;
there is no allocation or container growth in publication.

Normal-path events continue to publish live for one-second interval reporting.
Terminal gauges flush exactly once. Timeout-late work is deliberately retained
only in local telemetry and cannot reach stack-owned `RuntimeCounters`.

Regressions cover finalize waiting for an in-progress publication, rejecting a
late callback publication, a retained decoder-surface publication after the
external counters' scope, and the real encoder blocked-callback timeout path.

## C2: Reporter Failure and Backend Lifetime

- The interval thread stores its first exception. `stop_intervals()` and the
  controlling final path propagate it instead of silently terminating.
- `final_attempted_` is set before terminal sampling or writing. A failed,
  partial, or background-poisoned terminal path cannot be retried by catch
  cleanup or the destructor.
- A record still uses one `write()` call. Before writing a regular file the
  reporter records its end offset. A negative or partial result truncates back
  to that offset, closes the descriptor, and poisons further output. Thus a
  fragment is never left as an appendable JSONL record.
- The runtime catches reporter-stop failure without skipping native drain,
  attempts terminal handling once, propagates the cleanup error, and explicitly
  unbinds the backend.
- Destructor fallback drops the borrowed backend before any fallback final
  record, preventing unwind-order sampling through a destroyed backend.

Tests cover background partial propagation, actual file rollback, negative
final write propagation, no retry, one-write success records, and backend
borrow release.

## I1: Resource Availability

`BackendRuntimeSample::gpu_allocated_bytes` and reporter RSS samples are
optional. Unsupported sampling, sampler failure, and no-backend setup paths
serialize as JSON `null`. A supported measurement of zero serializes as the
numeric value `0`.

## Verification

Focused RED/GREEN verification:

```text
build/macos/swim_core_tests
99/99 passed

build/macos/metal_encoder_test
23/23 passed

build/macos/metal_preview_test
10/10 passed
```

Fresh full build and suite:

```text
cmake --build build/macos
ctest --test-dir build/macos --output-on-failure
```

Result: build succeeded; 10/10 CTest targets passed in 5.27 seconds.

Required runtime:

```text
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --stage=decode-render-encode --stream-count=6 --encode-sink=null \
  --duration-seconds=3 --metrics=/tmp/task2-fix-interval.jsonl
```

Result:

- 4 valid JSON lines: 3 intervals and exactly 1 final;
- final receive/decode/publish counts: `570/570/570`;
- final render/encode completions: `90/90`;
- hardware HEVC: `true`;
- decoded pixel host copies: `0`;
- RSS: `46776320` bytes and GPU allocation: `1926922240` bytes, both real
  numeric measurements;
- final cumulative encoder completions were at least the sum of interval
  deltas.

An independent fresh verification by the parent run also passed 10/10 CTest
targets and a 3-second six-camera hardware-encode run with exactly one final
record, 90 render/encode completions, zero host copies, and zero drain
timeouts.

## Concerns

- No TSAN run was available. The lifetime guarantee is instead covered by the
  single mutex protocol plus deterministic concurrency/timeout regressions.
- A real renderer or preview framework callback was not forcibly held beyond
  the five-second drain timeout; both now use the same tested publication
  protocol as the deterministic encoder timeout path.
- The mutex is intentionally uncontended in normal operation. If future
  telemetry adds slow work inside publication lambdas, the invariant that only
  short atomic operations run under the lock must be preserved.
