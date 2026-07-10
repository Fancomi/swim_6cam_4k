# Task 9 Implementation Report

## Result

Task 9 integrates the six latest-frame mailboxes with the offscreen Metal
render loop. The runtime explicitly registers the Metal backend, constructs one
shared Metal context for rendering and all VideoToolbox lanes, starts six source
adapters, renders one six-frame snapshot per absolute cadence tick, and performs
ordered signal-safe shutdown with a final JSON metrics line.

After the review fix wave, the final uncontended 10-second headless run submitted
and successfully completed 299 frames. Completion FPS was measured over the exact
steady-clock interval from the first accepted submit through the last
successful GPU completion: 29.950 fps. All six sources remained healthy, the
configured pools remained bounded, and decoded pixel host copies remained zero.

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
- typed accepted/not-ready/backpressure/fatal/invalid renderer results;
- resume sequence gaps after a displayed replacement frame;
- zero duration continuing until its stop token is requested;
- exact integer 30000/1001 cadence offsets without floating-point drift.

## Implementation notes

- `RenderCoordinator` owns six front leases and calls `consume_latest` exactly
  once per mailbox on each tick. Missing lanes reuse their front lease; source
  sequence gaps update overwrite metrics; renderer rejection increments
  `render_drops` without waiting for a surface.
- Last real source generation/sequence are independent of the displayed front
  and replacement state, so a resumed lane's skipped real sequences remain
  observable. Each selected real frame records a per-camera age sample.
- `IRenderer::submit` returns `RenderSubmitResult`. The coordinator continues
  only for `not_ready` and `backpressure`; native fatal and invalid snapshots
  terminate the render lane with an exception.
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
- The low Metal renderer optionally receives runtime counters. It reports both
  configured pool capacities, high-water marks and separate miss counts.
  Successful completion handlers update an atomic completion count and an
  atomic-max final steady timestamp so callback order cannot regress the FPS
  interval.
- The Metal backend owns one shared `MetalContext`, adapts Task 8's
  `Mp4VideoToolboxSource`, supplies stable black replacement leases, and is
  registered by an explicit referenced function rather than a discardable
  static initializer.
- SIGINT/SIGTERM handlers only set `sig_atomic_t`. An ordinary monitor thread
  requests the render stop and wakes the backend main loop. Sources then stop,
  the renderer drains, final fatal state is checked, and final metrics are
  written before any native error is rethrown.
- Runtime cleanup is owned by an ordered RAII finalizer. Mailboxes are
  constructed before source publishers; every post-start exit requests and
  joins render work, stops and joins every source, drains and checks the
  renderer, snapshots render-thread-only age histograms, writes final metrics,
  and then rethrows the original failure. Native sources always receive zero
  lane-local duration, leaving the global render interval/signal as stop owner.
- A process-wide `RunLifecycle` now owns activation, the finite deadline, and
  explicit stop. The coordinator activates it only after the first accepted
  submit; all MP4 lanes classify EOF against that same state. EOF before
  activation, before the deadline, or in an unbounded run before explicit stop
  is fatal and requests global stop. EOF at/after the deadline or after stop is
  normal. Track loading polls this lifecycle every 10 ms rather than waiting on
  an unbounded semaphore.
- A pointer-free final-metrics guard is installed immediately after runtime
  counters, before backend lookup or native construction. It emits exactly one
  final JSON line on backend, renderer, source-construction, source-start, and
  running failures. Per-lane start state is marked only after `start()` returns,
  and health includes only successfully started, nonfailed lanes.
- Metal drain is terminal and bounded to five seconds. A mutex/CV completion
  gate atomically closes against new submissions, while completion blocks hold
  `shared_ptr<Impl>` so late callbacks cannot access freed renderer pools or
  context. Callback-updated completion timestamps/counts remain Impl-owned and
  are flushed once during terminal drain, before external metrics can die.

## Verification

### Core and CLI regression

```text
cmake --build build/macos --target swim_core_tests swim_realtime -j8
build/macos/swim_core_tests
```

Result: build succeeded and every core test passed, including the expanded
render coordinator and metrics snapshot/reset contracts.

```text
ctest --test-dir build/macos --output-on-failure
```

Result: 8/8 tests passed. The added runtime setup test executes an unknown
backend failure and requires exactly one final JSON line with
`sources_healthy=0`.

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

Review-fix final metrics:

```json
{"final":true,"received":1836,"decoded":1836,"published":1836,"overwritten":24,"reused":3,"render_submissions":299,"render_completions":299,"render_drops":6,"render_active_ns":10000000000,"render_first_submit_ns":2716271520728125,"render_last_completion_ns":2716281504189375,"render_completion_interval_ns":9983461250,"render_fps":29.950,"render_inflight_capacity":3,"render_inflight_high_water":3,"render_inflight_pool_misses":1,"render_output_capacity":4,"render_output_high_water":3,"render_output_pool_misses":0,"frame_age_ms_p99":[31,32,33,30,33,33],"pool_exhaustion":1,"decoded_pixel_host_copies":0,"native_texture_wrappers":3672,"native_command_buffers":299,"native_decode_tickets":96,"sources_healthy":6,"output_width":5002,"output_height":2102}
```

The not-ready drops occurred during decoder startup before the first complete
six-frame snapshot. They are counted but excluded from the configured active
interval. All 299 accepted submissions completed successfully. The 96 native
decode tickets equal the fixed six-lane capacity of 16 tickets per lane;
configured in-flight/output capacities were 3/4, their high-water marks were
3/3, with one counted in-flight backpressure miss and no output-pool miss. All
six frame-age p99 values were at most 33 ms.

### Signal shutdown

A `duration-seconds=0` run was interrupted with SIGINT. It exited 0, reported
six healthy sources, and emitted its final metrics line after source stop and
renderer drain.
