# Task 11 Implementation Report

## Status

DONE_WITH_CONCERNS

Task 11 is implemented on `feature/realtime-metal` from baseline `55484e5`.
The encoder requires the hardware VideoToolbox HEVC path at exact
`5002x2102`, uses a capacity-two fixed input gate with stable preallocated
callback tickets, writes Annex-B HEVC to file or performs the full accounting
path for the null sink, and is integrated into the existing serial zero-copy
completed-output router.

## Commits

- `005d6cd feat: add bounded hardware HEVC encoder`
- `e625295 feat: integrate HEVC encoding metrics and runtime`
- `8c50625 fix: settle HEVC callback tickets before slot reuse`
- `c7b3bee fix: harden bounded HEVC callback shutdown`

Implementation commit range: `55484e5..c7b3bee`

## TDD RED Evidence

### Encoder primitives and hardware contract

Command:

```text
cmake --build build/macos --target metal_encoder_test
```

Observed RED before production implementation:

```text
cpp/tests/metal_encoder_test.mm:3:10: fatal error:
'swim/metal/metal_encoder.hpp' file not found
ninja: build stopped: subcommand failed.
```

The RED suite covered capacity-two nonblocking saturation, one/two/four-byte
NAL lengths, invalid widths, truncation and zero-length NAL rejection,
non-contiguous `CMBlockBuffer` boundaries, VPS/SPS/PPS ordering, callback-owned
output lease lifetime, bounded gate drain, exact dimensions, and hardware-only
session reporting.

### Metrics and runtime validation

Command:

```text
cmake --build build/macos --target swim_core_tests
```

Observed RED before metrics implementation:

```text
error: no member named 'encode_rejected_frames' in
'swim::core::RuntimeCounters'
error: no member named 'encode_submissions' in
'swim::core::RuntimeCounters'
error: no member named 'encode_completion_fps' in
'swim::core::MetricsSnapshot'
ninja: build stopped: subcommand failed.
```

The RED integration tests covered immutable snapshot/reset behavior, exact
counter values, cache-line separation, zero/regressing FPS intervals, `.h265`
and `.hevc` validation, `.h264` rejection, null-sink path exemption, and the
fixed `30000/1001` encode frame rate.

## Automated Verification

### Focused encoder test

```text
cmake --build build/macos --target metal_encoder_test
build/macos/metal_encoder_test
```

Result: PASS, 9/9 encoder cases, including
`hardware_encoder_requires_exact_canvas_and_reports_hardware`.

### Focused core test

```text
cmake --build build/macos --target swim_core_tests
build/macos/swim_core_tests
```

Result: PASS, all core cases including the new metrics/config cases.

### Full build and CTest

```text
cmake --build build/macos
ctest --test-dir build/macos --output-on-failure
```

Result: PASS, build exit 0; 10/10 CTests passed, 0 failed, total CTest time
3.92 seconds on the final code before the runtime acceptances.

`git diff --check` also exited 0.

## Five-Second File-Sink Acceptance

Commands:

```text
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --preview=false --encode=true \
  --encode-path=outputs/videos/pool_metal_5s.h265 \
  --duration-seconds=5 --metrics=/tmp/task11_5s.jsonl

ffprobe -v error -f hevc -select_streams v:0 \
  -show_entries stream=codec_name,width,height,r_frame_rate \
  -of default=noprint_wrappers=1 outputs/videos/pool_metal_5s.h265

ffmpeg -v error -f hevc -i outputs/videos/pool_metal_5s.h265 -f null -
```

Result: all commands exited 0. `ffprobe` reported:

```text
codec_name=hevc
width=5002
height=2102
r_frame_rate=1200000/1
```

`ffmpeg` decoded the entire elementary stream with no errors. Final runtime
metrics:

```text
render_submissions=150 render_completions=150 render_fps=30.115
encode_submissions=148 encode_completions=148 encode_drops=2
encode_rejected_frames=0 encode_callback_errors=0
encode_fps=30.029 encode_bytes=38562343
encode_input_capacity=2 encode_input_high_water=2 encode_input_in_use=0
encode_input_pool_misses=2 encode_using_hardware=true
encode_drain_timeouts=0 decoded_pixel_host_copies=0
output_width=5002 output_height=2102
```

## Thirty-Second Preview Plus Encode Acceptance

Command:

```text
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --preview=true --encode=true --duration-seconds=30 \
  --metrics=/tmp/task11_30s.jsonl
```

Result: exit 0. Final runtime metrics:

```text
render_submissions=894 render_completions=894 render_fps=29.827
render_inflight_capacity=3 render_inflight_high_water=3
render_output_capacity=4 render_output_high_water=4
preview_presents=890 preview_drops=4
encode_submissions=893 encode_completions=893 encode_drops=1
encode_rejected_frames=0 encode_callback_errors=0
encode_fps=29.887 encode_bytes=224200378
encode_input_capacity=2 encode_input_high_water=2 encode_input_in_use=0
encode_input_pool_misses=1 encode_using_hardware=true
encode_drain_timeouts=0 decoded_pixel_host_copies=0
output_width=5002 output_height=2102
```

The fixed renderer and encoder pools stayed within their configured
capacities. Output pressure appeared as bounded drops/misses; there was no
mailbox growth or decoded-pixel host copy.

## Self-Review

- Confirmed the encoder specification sets both require-hardware and
  enable-hardware keys and performs exactly one session creation attempt.
- Confirmed all required VideoToolbox properties are set and preparation plus
  `UsingHardwareAcceleratedVideoEncoder` are validated at startup.
- Confirmed file creation happens once before admission, null sink opens no
  file, and `encode=false` constructs no encoder.
- Confirmed each input record owns the output lease, PTS and submission
  sequence, and each stable callback ticket is allocated at startup.
- Confirmed callback settlement clears the callback ticket before publishing
  its pool slot as reusable. This review found and fixed a potential old/new
  callback-ticket overwrite race in `8c50625`.
- Confirmed renderer adapter shutdown retains the first exception while still
  draining renderer, router, preview and encoder in order.
- Confirmed adapter member order is `context_`, `router_`, `preview_`,
  `encoder_`, `renderer_` before native replacement members.
- Confirmed input H.264 decode checks were not changed and no software or
  resize fallback was added.

## Known Issues / Concerns

- Apple's raw VideoToolbox Annex-B elementary stream reports
  `r_frame_rate=1200000/1` through this ffprobe query even though frames are
  submitted with exact monotonic `sequence * 1001 / 30000` PTS and runtime
  completion rates are approximately 29.97 fps. Supplying an explicit
  `1001/30000` frame duration in a controlled experiment did not change the
  reported SPS/VUI rate, so that unrequired deviation was removed. Raw Annex-B
  carries no container timestamps for independent ffprobe PTS inspection.

## Independent Review Remediation (`2d630fb..c7b3bee`)

The formal Task 11 review findings C1, I1-I6, and M1 were repaired together in
`c7b3bee`.

### Finding-by-finding changes

- **C1 — late callback versus stack `RuntimeCounters`:** callback-reachable
  state now owns only encoder-local atomics, its fixed gate, writer operations,
  and an optional lifetime anchor. No callback path stores or dereferences a
  `RuntimeCounters` pointer/reference. `close_and_drain()` flushes the local
  snapshot exactly once while the external counters are alive, then sets the
  external pointer to null. The timeout test destroys both counters and encoder
  before injecting the late callback and proves final cleanup with a weak
  lifetime sentinel.
- **I1 — total drain deadline:** `CompleteFrames`, callback gate settlement,
  and writer close now run in one shutdown worker. The caller waits against one
  absolute deadline of two seconds in production. On timeout it records the
  timeout, invalidates the native session, disconnects/flushed external
  metrics, and returns. The worker retains the session, fixed tickets, and
  callback state until native completion, all callbacks, and writer close have
  actually settled. Deterministic tests use a zero deadline and condition
  variables to cover both blocked native completion and a callback blocked in
  the writer; no sleep is used for event ordering.
- **I2 — VideoToolbox frame-drop flags:** `infoFlagsOut` is supplied to
  `VTCompressionSessionEncodeFrame`; both synchronous and callback-side
  `kVTEncodeInfo_FrameDropped` are recoverable drops. Each path releases its
  output lease and gate ticket without setting fatal or callback-error state.
- **I3 — total drop accounting:** gate/closed/fatal rejections, native encode
  rejection, native frame drop, callback/sample/access-unit failure, writer
  append failure, and writer close failure increment `encode_drops`. A
  per-ticket atomic guard prevents the total from incrementing twice while
  `encode_rejected_frames` and `encode_callback_errors` remain reason counters.
- **I4 — writer failure:** file append and close are both checked. A short
  `fwrite` or nonzero `fclose` result sets fatal state, increments the callback
  error reason, and counts exactly one total drop. The append/close tests use
  injected in-memory operations and no filesystem.
- **I5 — fatal admission:** `MetalEncoder::offer()` immediately rejects after
  fatal state and counts the unproduced frame. The renderer adapter calls the
  same tested `metal_encoder_admits_render()` preflight before submitting new
  Metal work, returning `RenderSubmitResult::fatal` immediately.
- **I6 — synchronous callback timing:** first-submit time is published before
  the native encode call. A rejected first attempt rolls back its unqualified
  timestamp when no accepted submission/completion exists. A synchronous
  successful callback therefore always records `last_completion_ns` after
  `first_submit_ns`; the injected-clock test verifies nonzero `encode_fps`.
- **M1 — empty access unit:** both span and `CMBlockBuffer` Annex-B converters
  reject zero-byte access units. A mutation run restoring the old condition
  failed exactly at
  `length_prefixed_writer_rejects_an_empty_access_unit`, after which the fix was
  restored and the suite returned green.

### Review TDD RED evidence

The expanded encoder tests were first built against the pre-review production
interface:

```text
cmake --build build/macos --target metal_encoder_test
```

Observed RED:

```text
error: no type named 'MetalEncoderInjectedCallback' in namespace 'swim::metal'
error: no member named 'MetalEncoderInjectedOutputKind' in namespace 'swim::metal'
error: no type named 'MetalEncoderDependencies' in namespace 'swim::metal'
ninja: build stopped: subcommand failed.
```

The backend fatal-preflight test was then added before its helper and produced:

```text
error: no member named 'metal_encoder_admits_render' in namespace 'swim::metal'
```

The M1 mutation RED produced:

```text
FAIL length_prefixed_writer_rejects_an_empty_access_unit:
!swim::metal::write_length_prefixed_nals_as_annex_b({}, 4, writer)
```

### Review-focused and automated verification

```text
cmake --build build/macos --target metal_encoder_test swim_core_tests
build/macos/metal_encoder_test
build/macos/swim_core_tests
```

Result: PASS; 18/18 encoder cases plus all core cases. New deterministic cases
cover synchronous and asynchronous frame drops, native rejection, total/reason
drop accounting, synchronous completion timing, writer append failure, writer
close failure, fatal admission, total drain timeout, invalidation followed by a
late callback after external-object destruction, blocked callback writer, and
empty access units.

```text
cmake --build build/macos
ctest --test-dir build/macos --output-on-failure
git diff --check
```

Result: PASS; full build exit 0, 10/10 CTests passed, zero failures, and diff
check exit 0.

`swim_runtime_setup_failure_writes_final_metrics` passed as part of CTest and
continued to verify every encoder numeric field at zero,
`encode_using_hardware:false`, and `encode_codec:"hevc"`.

### Post-review five-second file sink

Commands:

```text
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --preview=false --encode=true \
  --encode-path=outputs/videos/pool_metal_5s.h265 \
  --duration-seconds=5 --metrics=/tmp/task11_review_5s_file.jsonl
ffprobe -v error -f hevc -select_streams v:0 \
  -show_entries stream=codec_name,width,height,r_frame_rate \
  -of default=noprint_wrappers=1 outputs/videos/pool_metal_5s.h265
ffmpeg -v error -f hevc -i outputs/videos/pool_metal_5s.h265 -f null -
```

Result: all exit 0; ffprobe reports HEVC `5002x2102`; ffmpeg decodes the full
stream without error. Metrics:

```text
render_fps=30.120 encode_fps=30.116
encode_submissions=149 encode_completions=149 encode_drops=1
encode_rejected_frames=0 encode_callback_errors=0
encode_bytes=38566861 encode_using_hardware=true
encode_input_capacity=2 encode_input_high_water=2 encode_input_in_use=0
encode_drain_timeouts=0 decoded_pixel_host_copies=0
```

### Post-review five-second null sink

Command:

```text
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --preview=false --encode=true --encode-sink=null \
  --duration-seconds=5 --metrics=/tmp/task11_review_5s_null.jsonl
```

Result: exit 0. The complete hardware encode and Annex-B accounting path ran
without file I/O:

```text
render_fps=30.113 encode_fps=29.993
encode_submissions=148 encode_completions=148 encode_drops=2
encode_rejected_frames=0 encode_callback_errors=0
encode_bytes=38564456 encode_using_hardware=true
encode_input_capacity=2 encode_input_high_water=2 encode_input_in_use=0
encode_drain_timeouts=0 decoded_pixel_host_copies=0
```

### Post-review thirty-second preview plus encode

Command:

```text
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --preview=true --encode=true --duration-seconds=30 \
  --metrics=/tmp/task11_review_30s.jsonl
```

Result: exit 0:

```text
render_fps=29.894 encode_fps=29.834
preview_presents=886 preview_drops=10
encode_submissions=892 encode_completions=892 encode_drops=4
encode_rejected_frames=0 encode_callback_errors=0
encode_bytes=224957243 encode_using_hardware=true
encode_input_capacity=2 encode_input_high_water=2 encode_input_in_use=0
encode_drain_timeouts=0 decoded_pixel_host_copies=0
output_width=5002 output_height=2102
```

The only remaining concern is the previously documented raw elementary-stream
ffprobe VUI rate (`1200000/1`). The independent review explicitly judged it
non-blocking for Task 11; submitted PTS generation remains the exact serial
`sequence * 1001 / 30000` contract.
