# Task 9 Implementation Report

## Result

Task 9 integrates the six latest-frame mailboxes with the offscreen Metal
render loop. The runtime explicitly registers the Metal backend, constructs one
shared Metal context for rendering and all VideoToolbox lanes, starts six source
adapters, renders one six-frame snapshot per absolute cadence tick, and performs
ordered signal-safe shutdown with a final JSON metrics line.

The final 10-second headless run rendered 300 frames over exactly
10,000,000,000 active nanoseconds (30.000 fps). All six sources remained
healthy, the configured pools remained bounded without exhaustion, and decoded
pixel host copies remained zero.

## TDD evidence

The coordinator test was written before production code and the required RED
build was observed:

```text
cpp/tests/test_render_coordinator.cpp:4:10: fatal error:
'swim/core/render_coordinator.hpp' file not found
ninja: build stopped: subcommand failed.
```

The coordinator tests now cover:

- independent stale-frame reuse without a cross-camera wait;
- replacement only after the configured one-second threshold;
- one submit attempt and a counted drop on renderer backpressure;
- a finite active duration beginning at the first accepted submit, so decoder
  warmup drops do not consume the requested render interval.

## Implementation notes

- `RenderCoordinator` owns six front leases and calls `consume_latest` exactly
  once per mailbox on each tick. Missing lanes reuse their front lease; source
  sequence gaps update overwrite metrics; renderer rejection increments
  `render_drops` without waiting for a surface.
- Realtime scheduling derives every deadline from one `steady_clock` epoch and
  the runtime `fps_den / fps_num` rational. The epoch resets at the first
  accepted render so a finite run measures the requested active interval.
  Benchmark mode submits again immediately after acceptance and uses a
  stop-aware 1 ms backoff only when the fixed renderer pool rejects a submit.
- `FrameLease::backend_tag()` makes native type selection explicit. The Metal
  renderer switches on the tag, uses `MetalDecodedSurface::view` for decoded
  frames, and never layout-casts a decoded surface to `MetalFrameView`.
- Every accepted command copies all six input leases into its fixed in-flight
  record. The completion handler clears those leases only after the Metal
  command buffer completes.
- The Metal backend owns one shared `MetalContext`, adapts Task 8's
  `Mp4VideoToolboxSource`, supplies stable black replacement leases, and is
  registered by an explicit referenced function rather than a discardable
  static initializer.
- SIGINT/SIGTERM handlers only set `sig_atomic_t`. An ordinary monitor thread
  requests the render stop and wakes the backend main loop. Sources then stop,
  the renderer drains, final fatal state is checked, and final metrics are
  written before any native error is rethrown.

## Verification

### Core and CLI regression

```text
cmake --build build/macos --target swim_core_tests swim_realtime -j8
build/macos/swim_core_tests
```

Result: build succeeded and every core test passed, including all four render
coordinator tests and the expanded metrics snapshot/reset contract.

```text
ctest --test-dir build/macos --output-on-failure
```

Result: 7/7 tests passed.

### Metal golden regression

```text
build/macos/metal_golden_test assets/generated/pool_4k.swasset \
  /tmp/task9_golden.png \
  inputs/textures/camera_3_composite.png \
  inputs/textures/camera_2_composite.png \
  inputs/textures/camera_1_composite.png \
  inputs/textures/camera_4_composite.png \
  inputs/textures/camera_5_composite.png \
  inputs/textures/camera_6_composite.png
```

Result: exit 0; exact GPU completion timestamps were reported and the PNG was
written.

### Six-lane native decode regression

The six-camera `metal_decode_probe` ran for two seconds. Every lane reported
hardware decoding, approximately 29.97 measured fps, zero decoder drops, and
zero host copies.

### Required headless smoke

```text
build/macos/swim_realtime --config configs/macos_20260629.conf \
  --preview=false --encode=false --duration-seconds=10
```

Final metrics:

```json
{"final":true,"received":1794,"decoded":1794,"published":1794,"overwritten":220,"reused":229,"render_submissions":300,"render_drops":5,"render_active_ns":10000000000,"render_fps":30.000,"pool_exhaustion":0,"decoded_pixel_host_copies":0,"native_texture_wrappers":3588,"native_command_buffers":300,"native_decode_tickets":96,"sources_healthy":6,"output_width":5002,"output_height":2102}
```

The five drops occurred during decoder startup before the first complete
six-frame snapshot. They are counted but excluded from the configured
10-second active render interval. The 96 native decode tickets equal the fixed
six-lane capacity of 16 tickets per lane; configured decoded surfaces,
in-flight renders, and output surfaces remained bounded at 8 per lane, 3, and
4 respectively.

### Signal shutdown

A `duration-seconds=0` run was interrupted with SIGINT. It exited 0, reported
six healthy sources, and emitted its final metrics line after source stop and
renderer drain.
