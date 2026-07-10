# Real-Time Six-Camera 4K GPU Stitching Design

Date: 2026-07-10

## 1. Purpose

Build a standalone, production-oriented renderer for six real 4K cameras. The
system must prioritize fresh frames and predictable latency over preserving
every frame. It must keep decoded images on the GPU, perform deformation,
feathering, composition, preview, and encoding without CPU image round trips,
and expose enough stage-level metrics to locate throughput limits.

The first implementation and performance experiment runs on the current Apple
M5 MacBook Pro with VideoToolbox and Metal. Once that path is stable, the same
core interfaces will receive an Ubuntu backend based on NVDEC, CUDA/EGL/OpenGL,
and NVENC.

This repository remains independent. The existing
`sport-detect-haotian` repository and its architecture are not modified.

## 2. Goals

- Accept six `3840x2160` H.264 streams at `30000/1001` fps.
- Render the latest complete frame from each camera without waiting for the
  other cameras.
- Produce a `5002x2102` GPU-resident composite at `30000/1001` fps. The
  existing `5001x2101` image occupies the top-left content region; one column
  and one row are padding for an even-sized H.264 surface.
- Preserve the existing FBX-derived geometry, camera order, UV mapping, mirror
  sampling, and distance-transform feathering semantics.
- Keep the production per-frame path entirely in C++ and platform-native GPU
  APIs.
- Allow runtime backend selection among the backends compiled into the binary.
- Bound every queue and resource pool so downstream stalls cannot cause
  unbounded latency or memory growth.
- Report decode, exchange, render, preview, and encode behavior separately.
- Measure both real-time behavior and unconstrained maximum throughput for
  1, 2, 4, and 6 input streams.

## 3. Non-Goals

- Frame-accurate PTP synchronization or waiting for matching timestamps.
- Preserving every decoded or rendered frame.
- Using Python, OpenCV, ROS, FFmpeg, GStreamer, or CPU pixel arrays in the
  production hot path.
- Reworking the old venue-deployment or annotation repositories.
- Implementing the Ubuntu backend before the Metal experiment is correct and
  stable.
- Parsing FBX or calculating remaps and feather masks at runtime.

FFmpeg and OpenCV may remain in offline validation scripts only. They are not
runtime fallback backends.

### 3.1 Alternatives Considered

Three approaches were evaluated:

1. **Native end-to-end backends behind a small C++ core contract — selected.**
   Metal/VideoToolbox and EGL/CUDA/NVIDIA can each preserve native resource
   ownership and reach their platform's highest practical performance.
2. **One Vulkan renderer on both systems.** MoltenVK would share more rendering
   code, but VideoToolbox and NVDEC import/encode interop would remain
   platform-specific and add another translation layer to the local experiment.
3. **A unified FFmpeg, GStreamer, or OpenCV pipeline.** This would reduce initial
   code but make implicit queues, hidden host copies, pixel conversions, and
   latest-frame behavior harder to control and measure.

The selected approach accepts two small native GPU implementations in exchange
for explicit memory lifetime, backpressure, and copy behavior. The shared core
does not attempt to abstract pixels or GPU commands.

## 4. Selected Architecture

The production application is one C++ process. A backend owns the complete
native path from compressed input through hardware decode, GPU composition,
preview, and hardware encode. The platform-neutral core owns configuration,
camera identity, latest-frame policy, orchestration, state transitions, and
metrics.

```text
six compressed sources
        |
        v
six independent hardware-decode lanes
        |
        v
six SPSC LatestFrameMailboxes (latest complete frame only)
        |
        v
one render coordinator and one GPU queue/context
        |
        v
six mesh draws into one 5002x2102 GPU output surface
        |                         |
        v                         v
asynchronous preview       asynchronous hardware encoder
```

The application does not render six intermediate camera images and does not
read them back for a CPU stitching pass. Static geometry and feather assets are
uploaded once at startup.

## 5. Repository and Language Boundaries

The implementation uses the following boundaries:

```text
cpp/
  app/                    CLI, startup, shutdown, lifecycle
  core/                   backend-neutral contracts and orchestration
  backends/
    metal/                Objective-C++/C++ VideoToolbox and Metal backend
    egl_cuda/             C++ NVDEC, CUDA/EGL/OpenGL and NVENC backend
  tests/                  C++ unit, concurrency, and integration tests
python/
  assets/                 FBX/JSON to runtime-asset compiler
  validation/             golden-image and benchmark analysis
assets/                   versioned runtime mesh and feather packages
configs/                  runtime configurations
benchmarks/               machine-readable results and generated reports
```

`cpp/core` must not include Metal, CoreVideo, CUDA, EGL, OpenGL, or NVENC
headers. Backend-specific native resource types stay inside their backend.
Python and C++ communicate through versioned files: asset packages,
configuration, golden images, and benchmark JSON. Live frames never cross the
Python/C++ boundary.

## 6. Core Contracts

### 6.1 Backend

Compiled backends register factories under stable names. The initial name is
`metal`; the Ubuntu backend will use `egl-cuda`. `--backend <name>` selects a
registered backend at runtime. Selecting an unavailable backend is a fatal
startup error that lists the compiled choices.

Each backend implements these responsibilities behind narrow core contracts:

- create and validate sources;
- create one decode lane per source;
- publish immutable native-frame leases;
- load the common runtime asset package;
- render a six-frame snapshot;
- present and/or encode the final native output surface;
- expose timings and fixed-pool occupancy.

The backend boundary is control-oriented. It does not expose generic byte
buffers or require native GPU handles to be converted into a common pixel
representation.

### 6.2 Frame Metadata and Native Lease

Every decoded frame has immutable metadata:

- camera ID;
- per-camera sequence number;
- source presentation timestamp when available;
- monotonic host arrival and decode-completion timestamps;
- width, height, pixel format, and color description;
- discontinuity and decoder-generation flags.

The decode lane assigns a monotonically increasing display sequence. A late
callback or regressing presentation timestamp from an older decoder generation
is discarded rather than replacing a newer published frame. The mailbox never
uses timestamps to wait for or align different cameras.

A backend-native lease retains the decoded surface until every submitted GPU
use completes. The core can inspect metadata and move lease ownership, but
cannot access pixels. Metal leases retain `CVPixelBuffer`/`CVMetalTexture`
objects. The Ubuntu backend will retain its NVDEC/CUDA/EGL resources and defer
release until the relevant fence signals.

### 6.3 LatestFrameMailbox

Each camera owns one single-producer/single-consumer triple-buffer mailbox.
Producer, middle, and consumer slots exchange using atomic indices and
acquire/release ordering. The producer can repeatedly replace unpublished or
unconsumed frames without touching the consumer's front slot. The consumer
atomically adopts the most recently published complete generation and skips
all intervening generations.

The mailbox contains small descriptors and native leases, not pixel storage.
It has these semantics:

- publishing never waits for the render thread;
- reading never waits for the decode lane;
- the renderer reuses its current front frame when no new generation exists;
- skipped and overwritten generations are counted;
- a front frame cannot be returned to the decoder pool until the backend's GPU
  completion primitive releases it.

Cache-line padding separates producer state, consumer state, and high-rate
counters to avoid false sharing.

## 7. Threading and Backpressure

Each source has an independent native ingest/decode lane. A slow, corrupt, or
reconnecting source cannot block another lane. Native decode callbacks publish
directly to the camera mailbox; there is no shared decoded-frame queue.

One render thread snapshots all six mailboxes, records the selected generation
and frame age for each camera, and submits one composite operation to one GPU
queue/context. It never waits for all cameras to advance. The render tick runs
at `30000/1001` fps in real-time mode or as fast as resources permit in
benchmark mode.

Preview and encoding are separate asynchronous consumers of the final output
surface. Their surface pools have fixed capacities. When either output cannot
accept a new frame, that output drops the pending composite; it does not block
decode or render. Rendering also has a fixed in-flight limit enforced by GPU
completion primitives.

All access-unit buffers, descriptor pools, command-support objects, and output
surfaces are created
during startup or warm-up. Production measurements must not perform a
per-frame heap allocation. Pool exhaustion increments a metric and applies the
documented drop policy; it never grows the pool implicitly.

## 8. GPU Data Paths

### 8.1 macOS Metal

VideoToolbox produces NV12 `CVPixelBuffer` surfaces. The backend maps both
planes to Metal textures with `CVMetalTextureCache`. No BGR or RGB image is
materialized on the CPU.

Final output surfaces come from a fixed IOSurface-backed `CVPixelBufferPool`.
The same surface can be a Metal render target, a preview texture, and a
VideoToolbox encoder input. Command-buffer completion handlers retain all six
input leases and the output lease until GPU work completes.

The local file experiment accepts the six raw H.264 dataset streams. An Annex-B
parser supplies access units to VideoToolbox. When an elementary stream has no
usable presentation timestamps, real-time replay assigns timestamps from frame
index at `30000/1001`; benchmark replay is unpaced.

### 8.2 Ubuntu EGL/CUDA

The future backend uses one decoder per camera and keeps decoded NV12 surfaces
in device memory. It first attempts direct native import into the render path.
If the deployed driver cannot import an NVDEC surface for sampling, the only
permitted fallback is one device-to-device plane transfer into persistent
CUDA/OpenGL-interoperable textures. Host staging is prohibited.

One EGL context owns the final framebuffer and all six persistent sampling
textures. The final surface is shared with NVENC without `glReadPixels`.

## 9. Runtime Asset Package and Rendering Semantics

The current FBX/JSON remains source material. A Python compiler creates a
versioned runtime package containing:

- package version, checksum, camera identities, and camera order;
- indexed vertex positions and UV coordinates;
- per-camera coverage bounds;
- six normalized distance-transform feather-weight textures;
- logical and encoded canvas dimensions;
- world-to-canvas, V-axis, pixel-center, texture-addressing, and color-space
  metadata.

The six-camera mesh order is explicitly stored as camera 3, camera 2, camera 1,
camera 4, camera 5, and camera 6. It is never inferred from filenames or C++
container iteration order.

At startup, the backend validates the asset checksum and supported version,
then uploads all static buffers and textures once. A frame uses a single FP16
accumulation target. Each camera draw performs:

1. NV12-plane sampling;
2. shader YUV-to-linear-color conversion using the frame color description;
3. FBX UV deformation and bilinear mirrored sampling;
4. multiplication by the precomputed normalized feather weight;
5. additive accumulation into the final content region.

Because feather weights are normalized per output pixel, overlap blending is
order-independent and single-coverage pixels retain full intensity. The pool
center remains a hard seam where the two banks do not overlap. The extra right
column and bottom row are padding and do not change the `5001x2101` projection.

Metal Shading Language and GLSL implementations are separate, small backend
files governed by the same sampling/color contract and golden tests.

## 10. Run Modes and Configuration

The same executable supports:

- `realtime`: inputs are paced, the renderer targets `30000/1001` fps, and the
  latest-frame policy models live cameras;
- `benchmark`: pacing is disabled and a chosen stage combination runs at
  maximum sustainable throughput.

Configuration names the backend, six camera sources, runtime asset package,
render rate, preview/encode outputs, stale thresholds, fixed-pool capacities,
and benchmark duration. Command-line switches can override scalar settings for
experiments, but the resolved configuration is written into every result.

The initial source type is an H.264 elementary-stream file. The later Ubuntu
camera source implements the same source contract and produces identical frame
metadata. No live-source behavior leaks into the renderer.

## 11. Failure Handling

Each camera transitions independently through:

```text
starting -> healthy -> stale -> reconnecting -> healthy
                         `----> failed
```

The default configuration marks a source stale after 100 ms without a new
decoded frame and continues to reuse its last frame. After one second, the
backend replaces that camera with a black surface and reconnects only that
source. A diagnostic color replacement is available only under an explicit
diagnostic flag. Recoverable reconnect attempts use exponential delays from
250 ms through 5 s and continue indefinitely; an unsupported codec or invalid
source configuration transitions that lane to `failed`. Resolution or
codec-parameter changes rebuild only the affected decode lane while its last
valid front frame remains available.

Asset validation failure, unsupported backend capability, inability to create
the required fixed pools, and GPU device loss are fatal. The process records a
structured reason and exits instead of silently entering a CPU path.

Preview or encoder saturation is non-fatal and applies the output-drop policy.
Malformed input is isolated to its camera lane. Clean shutdown stops new input,
drains or cancels bounded in-flight GPU work, releases leases, and writes the
final report.

## 12. Observability

High-rate measurements use monotonic clocks and backend GPU timestamp queries.
The application prints a one-second summary and writes a JSONL result stream
containing:

- per-camera receive, submitted, decoded, published, overwritten, reused,
  malformed, and reconnect counts;
- per-camera latest-frame age and `p50`, `p95`, and `p99` summaries;
- maximum frame-age spread in each six-camera render snapshot;
- CPU input/decode-submission time and GPU composition/encoding time;
- render, preview, and encode FPS;
- render and encode in-flight counts;
- every fixed pool's current/high-water occupancy;
- decoded-pixel host-copy count, expected to remain zero in production mode;
- process memory and backend-reported GPU memory high-water values;
- resolved configuration, machine identity, OS, compiler, build type, backend,
  asset checksum, and source checksums.

CPU readback, debug screenshots, validation overlays, and synchronous GPU
timing are available only behind explicit diagnostic flags. Reports produced
with such flags are marked non-production and cannot satisfy performance gates.

## 13. Verification and Performance Matrix

### 13.1 Core Correctness

- Unit tests cover asset schema validation, camera ordering, generation
  selection, stale-state transitions, bounded-pool behavior, and drop counts.
- The triple-buffer mailbox runs millions of mixed-rate exchanges under normal
  tests and under ASan, UBSan, and TSan.
- Stress tests prove that the consumer never observes a partially published
  descriptor and that a native lease is not released before simulated GPU
  completion.

### 13.2 Image Correctness

Six fixed lossless camera inputs are rendered by the GPU and the existing
Python reference. The `5001x2101` content region must achieve both
`SSIM >= 0.995` and `PSNR >= 45 dB`. Difference images separately inspect
geometry edges, mirror borders, overlap seams, center seam, and padding.

VideoToolbox NV12 output is also compared against decoded reference frames so
color-range or matrix errors cannot be mistaken for geometric errors.

### 13.3 Performance Matrix

Every build measures 1, 2, 4, and 6 streams in these modes:

- hardware decode only;
- synthetic resident-texture render only;
- decode plus render;
- decode, render, and preview;
- decode, render, and hardware encode;
- complete preview and encode together.

Each cell records real-time results and unpaced maximum throughput. Short
profiling runs identify bottlenecks; production acceptance uses a ten-minute
soak after warm-up.

### 13.4 Local Metal Acceptance

The Metal milestone is accepted when:

- six `3840x2160@30000/1001` streams remain active for ten minutes;
- `5002x2102@30000/1001` preview is sustained for the full run;
- production decode-to-render decoded-pixel host-copy count is zero; compressed
  access-unit movement is measured separately and is not counted as an image
  copy;
- decode and render never wait for another camera, preview, or encoding;
- per-camera latest-frame-age `p99` is at most two input frame periods;
- production hot-path per-frame heap allocation count is zero after warm-up;
- all surface and in-flight counts stay within configured fixed limits;
- process and GPU memory show no sustained growth or leaked resources;
- no partial, corrupt, or misidentified frame reaches the renderer;
- hardware-encode mode reports whether it independently sustains
  `30000/1001` fps and identifies the limiting stage if it does not.

The unconstrained six-stream result is reported even when it exceeds the
real-time requirement; 30 fps is not treated as the performance ceiling.

## 14. Delivery Stages

1. Define the runtime asset package, compile current geometry/weights, and
   establish Python golden outputs.
2. Implement the platform-neutral C++ contracts, fixed pools, triple-buffer
   mailbox, metrics, and concurrency tests.
3. Implement Metal rendering with synthetic resident textures and pass the
   still-image golden tests.
4. Implement Annex-B input and VideoToolbox decode for 1, then 2, 4, and 6
   streams.
5. Add latest-frame real-time replay, preview, hardware encoding, the full
   performance matrix, and the ten-minute soak.
6. Profile and optimize only measured bottlenecks until the local acceptance
   gates pass or the hardware ceiling is documented stage by stage.
7. In a separate Ubuntu implementation cycle, add the `egl-cuda` backend and
   repeat the same correctness and performance matrix on the deployment GPU.

Stages 1 through 6 form the first implementation plan. Stage 7 starts only
after the Metal results and target Ubuntu hardware/driver details are
available.

## 15. Principal Risks and Mitigations

- **VideoToolbox surface retention reduces decoder throughput.** Use fixed
  in-flight limits, release on command completion, and expose pool high-water
  metrics before tuning pool size.
- **NV12 color interpretation differs across decoders.** Carry range, matrix,
  and primaries in frame metadata and test color separately from geometry.
- **CPU reference and GPU bilinear sampling disagree at pixel centers.** Store
  coordinate and mirror-address rules in the asset contract and use focused
  edge golden tests.
- **Instrumentation changes the measured result.** Use asynchronous counters
  and GPU timestamp queries; mark readback/synchronous diagnostic runs as
  non-production.
- **A portable abstraction introduces copies.** Keep the shared interface at
  control and metadata level; each backend owns its native end-to-end resource
  path.
- **The macOS result does not predict NVIDIA performance exactly.** Preserve
  the test matrix and stage definitions so the Ubuntu backend is measured by
  the same contract instead of extrapolating from Metal.
