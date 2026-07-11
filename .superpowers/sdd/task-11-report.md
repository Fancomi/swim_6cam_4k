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

Commit range: `55484e5..8c50625`

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
- The automated suite directly covers gate timeout and callback-owned lease
  settlement, while the true late-VideoToolbox-callback-after-timeout path is
  guarded by shared callback ownership and session invalidation but was not
  forcibly induced on this hardware.

