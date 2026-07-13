# Task 7 Report: Metal Static-Texture Stitching and Golden Validation

## Status and Commit

- Status: complete
- Branch: `feature/realtime-metal`
- Base: `c1fb38a49d8333eb91eca41f518cebadac646443`
- Initial implementation: `e155962f40200f9ff2848d54ee9114b4edd8440b`
  (`feat: render stitched assets with Metal`)
- Reviewer gate fix: `24a54fe` (`test: enforce local Metal golden boundaries`)
- First Metal/CMake reviewer fix: `92bf3be`
  (`fix: harden Metal golden validation`)
- Byte-exact geometry/race reviewer fix: this report's follow-up commit

## Delivered

- Added backend-local `MetalContext`, `MetalFrameView`, output slots, copyable
  refcounted output leases, fixed output pool, and `MetalRenderResult`. No Apple
  type or framework header was added under `cpp/core`.
- `MetalOutputPool` preallocates `1..64` IOSurface-backed BGRA8
  `CVPixelBuffer`/`CVMetalTexture` pairs, returns `std::nullopt` on exhaustion,
  records high-water occupancy, cleans partial construction failures, and owns
  a shared `MetalContext`.
- The lifetime contract is explicit and enforced: the pool/context must outlive
  leases; copies increment the stable slot's atomic reference count; destruction
  with any outstanding reference calls `std::terminate` before native resources
  or slots are destroyed. The golden executable tests copy retention,
  exhaustion, recycling, and high-water behavior.
- Added `MetalStitchRenderer` for both six `MetalFrameView` values and the core
  `RenderSnapshot`. It validates the exact camera order
  `cam3,cam2,cam1,cam4,cam5,cam6` and rejects either input form when a frame's
  metadata camera index does not equal its array slot.
- Startup compiles `stitch.metal` once through `newLibraryWithSource`, with safe
  math and an absolute deterministic `SWIM_METAL_SHADER_SOURCE_PATH` CMake
  definition. No standalone `metal` or `metallib` tool is required.
- Startup uploads six vertex buffers, index buffers, and cropped R16_UNORM
  weight textures; it also preallocates the configured FP16 accumulation
  textures, fixed in-flight records, and IOSurface output pool.
- Submission is non-blocking on in-flight/output exhaustion. It retains all
  input views, core input leases, and one output-lease copy until the Metal
  completion handler runs. Production submission does not wait, lock a pixel
  buffer, read pixels, or increment a decoded-pixel host-copy counter.
- The only per-submit objects are framework-managed Metal command buffers,
  encoders/descriptors, completion blocks, and the driver's inline
  `set*Bytes` storage. Application-owned in-flight records and surfaces remain
  bounded startup allocations.
- Diagnostic results retain their exact submitted command buffer. Diagnostic
  waits inspect its completion status/error and derive timestamps from that
  command rather than renderer-global timing state. The asynchronous completion
  handler exposes any production GPU failure through `has_fatal_error()`.
- Added the requested ImageIO/CoreGraphics diagnostic executable. It loads the
  six PNGs in exact camera order, renders one frame, waits only in the diagnostic
  path, writes the padded PNG, and reports Metal GPU timestamps.
- Added offline OpenCV/NumPy comparison with exact candidate dimensions
  `5002x2102`, exact black/opaque padding, global `45/0.995` PSNR/SSIM gates,
  local center/last-row/last-column `1.25/3.75` MAE/RMSE gates, explicit
  neighbor-duplication rejection, nonzero failure exit, and a 4x amplified
  `<candidate>_diff.png`.
- `metal_golden_test` depends on `runtime_asset`, including when the generated
  asset is absent at the start of a focused target build.

## Shader and Pixel-Center Decisions

- One `stitch_vertex` function is shared by `stitch_rgba`, `stitch_nv12`, and
  `resolve_accumulation` pipelines.
- Geometry receives a `+0.5` pixel-center offset. RGBA coordinates use
  `(u, 1-v) + 0.5/textureSize`; the diagnostic loader preserves top-to-bottom
  PNG rows, avoiding a second vertical inversion.
- Source textures use normalized linear mirrored-repeat sampling to match
  `cv2.BORDER_REFLECT_101`; cropped weights use normalized linear clamp
  sampling at exact output pixel centers.
- RGBA and NV12 fragments multiply target R'G'B' by the sampled normalized
  weight and return weight in alpha. The render target is `RGBA16Float` with
  source-one/destination-one additive blending.
- NV12 implements BT.709 video/full-range conversion selected from metadata;
  BT.601/BT.2020 coefficients are also selected for their existing metadata
  enum values. Luma uses a half-luma-texel center and the half-resolution
  chroma plane uses one luma texel in normalized coordinates. Full-range chroma
  uses `(CbCr - 128/255) * (255/254)` and is covered by neutral and non-neutral
  synthetic textures through the actual runtime-compiled Metal shader path.
- Resolve divides accumulated RGB by alpha, clamps to `[0,1]`, emits BGRA8,
  and forces the padded `x=5001` column and `y=2101` row black.
- Mesh coordinates are pixel indices and the vertex shader's `+0.5` maps them
  to pixel centers. Static MTL vertex buffers are uploaded byte-for-byte from
  the asset, preserving positions and UVs. To match OpenCV's inclusive polygon
  perimeter under Metal's top-left raster rule, startup records each mesh's
  actual min/max bounds and the vertex shader changes only clip/raster position
  by at most `1/16` pixel for perimeter vertices within a `1/64`-pixel
  tolerance. It never snaps geometry to weight bounds; resolve disables the
  expansion uniform. This covers real `x=5000`, `y=2100`, and both weighted
  banks at `y=1050` while leaving uploaded geometry, UVs, indices, and weights
  unchanged.
  Resolve reads the same accumulation coordinate with no neighbor substitution;
  only encoded padding `x=5001` and `y=2101` is forced black.

## TDD and Debugging Evidence

1. Baseline before edits: CTest passed `7/7`; stdlib Python unittest passed
   `11/11`. The environment lacks the optional `pytest` module, so the
   repository's existing unittest runner was used.
2. Golden target RED: `cmake --build build/macos --target metal_golden_test`
   exited 1 at `metal_golden_test.mm:1` with
   `'swim/metal/metal_renderer.hpp' file not found`.
3. Output-pool GREEN: the target compiled/linked and the executable's focused
   capacity-one pool checks passed after the backend-local frame/pool slice.
4. Renderer RED: after adding full ImageIO-to-PNG diagnostic behavior, the
   same target exited 1 at link with undefined `MetalStitchRenderer`
   constructor, destructor, `submit`, and `wait_for_completion` symbols.
5. Renderer GREEN: the implemented renderer compiled and linked. The first
   runtime shader compile exposed MSL's reserved `vertex` keyword as a local
   identifier; the minimal rename made the runtime pipeline execute.
6. Comparator RED: `.venv/bin/python -m unittest
   python.tests.test_compare_images -v` exited 1 with
   `ModuleNotFoundError: python.validation.compare_images`.
7. Comparator GREEN: two focused tests passed for perfect padded cropping,
   exact SSIM, threshold rejection, and amplified difference output.
8. Golden iterations kept thresholds fixed: initial double-inverted diagnostic
   PNG load was `PSNR=16.041840, SSIM=0.664764646`; correcting only the loader
   row orientation gave `40.824749, 0.998925107`; resolving the measured
   inclusive logical boundaries gave `49.732816, 0.999894764`.
9. Reviewer metadata RED: the focused executable exited 1 with
   `direct Metal views accepted camera metadata in the wrong slot`; direct and
   snapshot negative cases pass after slot validation was added.
10. Reviewer raster RED: the old artifact reported exact duplicates for both
    `y=2100 == y=2099` and `x=5000 == x=4999`. Position-only perimeter
    expansion made both fingerprints false without changing UVs or weights.
11. Reviewer NV12 RED: the actual shader-path neutral texture failed with
    `full-range NV12 neutral chroma is not exactly neutral`; the specified
    `128/255` center and `255/254` scale made neutral and non-neutral cases pass.
12. Comparator regression tests demonstrate that localized `+32` corruption
    can retain globally passing metrics (`51.252064/0.999940874` at the center,
    `55.018373/0.999969107` at the last column) while failing the local gates.
13. Byte-exact geometry RED: the focused target failed to link with undefined
    exact-upload and expansion diagnostics. The GREEN executable verifies every
    MTL vertex buffer byte against the asset (including UVs), checks shader-only
    expansion is at most `1/16` pixel, and uses cam5 to detect the rejected
    greater-than-one-pixel weight-bound snap.
14. Completion/drain race review removed the shared non-atomic ARC command
    field from in-flight records. Diagnostic results retain their exact command;
    production `drain()` observes only atomic `busy`, whose completion handler
    release-stores false after retained resources are cleared.

## Files

- Modified: `CMakeLists.txt`
- Added: `cpp/backends/metal/include/swim/metal/metal_frame.hpp`
- Added: `cpp/backends/metal/include/swim/metal/metal_renderer.hpp`
- Added: `cpp/backends/metal/src/metal_renderer.mm`
- Added: `cpp/backends/metal/shaders/stitch.metal`
- Added: `cpp/tests/metal_golden_test.mm`
- Added: `python/validation/compare_images.py`
- Added: `python/tests/test_compare_images.py`

## Fresh Verification

- `cmake --build build/macos` — exit 0, no build warnings.
- `ctest --test-dir build/macos --output-on-failure` — `7/7` passed.
- `.venv/bin/python -m unittest discover -s python/tests -v` — `19/19`
  passed, including all eight strict comparator tests.
- Clean focused dependency check: removed
  `assets/generated/pool_4k.swasset`, then
  `cmake --build build/macos --target metal_golden_test -j` regenerated the
  asset and exited 0.
- Exact brief command:
  - `cmake --build build/macos --target metal_golden_test` — exit 0.
  - `build/macos/metal_golden_test assets/generated/pool_4k.swasset
    /tmp/pool-metal-byte-exact-final.png` plus the six required PNGs — exit 0,
    including output-pool lifetime, direct/snapshot metadata negatives, neutral
    and non-neutral full-range NV12 shader tests, byte-exact uploaded geometry,
    bounded shader-only expansion, exact command completion, and output
    `5002x2102`; GPU timestamps
    `2669136374733000..2669136375875166 ns` for the final run.
  - `.venv/bin/python -m python.validation.compare_images
    outputs/images/pool.png /tmp/pool-metal-byte-exact-final.png` — exit 0,
    `PSNR=50.183620`, `SSIM=0.999908165`; center MAE/RMSE
    `0.516830/0.722700`, last row `1.176165/2.075557`, last column
    `0.592734/1.550974`; both neighbor-duplication fingerprints false;
    difference image `/tmp/pool-metal-byte-exact-final_diff.png`.
- `git diff --check` passed before commit.

## Self-Review and Concerns

- Re-read the Task 7 brief and clarifications against all eight changed files.
  Static resource upload, fixed surfaces, nonblocking backpressure, exact camera
  order, diagnostic-only readback, Apple-header isolation, and exact numeric
  gates are covered.
- The RGB static path and both shader pipelines compile on the target Metal
  runtime. Full-range NV12 is numerically exercised with R8/RG8 synthetic Metal
  textures; Task 8 will add live VideoToolbox `CVMetalTexture` lifetime coverage.
- Runtime source compilation adds a deterministic one-time startup cost and
  keeps shader errors at startup rather than build time, as required by the
  available Command Line Tools environment.
- `MetalRenderResult` GPU timestamps and errors are populated by the exact
  command retained for diagnostic wait. Production submission remains
  asynchronous and allocation-bounded, and exposes fatal GPU status without
  diagnostic readback.
- In-flight records contain no command-buffer ARC field. This avoids concurrent
  retain/release access between completion and `drain()`; bounded record lifetime
  is synchronized only through the atomic busy flag.
- The feature branch/worktree is preserved for subsequent tasks; no merge,
  push, or cleanup was performed.
