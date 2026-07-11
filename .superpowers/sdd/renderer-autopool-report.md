# Renderer autorelease-pool hypothesis report

## Scope

Single-variable test of this hypothesis:

> The render `std::jthread` has no per-submission Objective-C autorelease pool,
> so autoreleased Metal command objects retain distinct decoded textures and
> cause the observed GPU-allocation growth.

The test changed only the two public `MetalStitchRenderer::submit` Objective-C
submission boundaries. It did not change decoder code, waits, caches, resource
pools, queue capacities, or completion behavior.

## RED evidence

Baseline commit: `03eb5b7825a51db624c87219f59c045e259c9bb1`

Command:

```bash
env OBJC_DEBUG_MISSING_POOLS=YES build/metal-release/swim_realtime \
  --config configs/macos_20260629.conf \
  --stage=decode-render --stream-count=6 --mode=realtime \
  --duration-seconds=15 --preview=false --encode=false \
  --metrics=/tmp/renderer-red.jsonl
```

The runtime emitted 581 `MISSING POOLS` diagnostics. Code-path inspection also
showed that `MetalStitchRenderer::submit` created the command buffer, pass
descriptors, and encoders on the render `std::jthread` without a per-frame pool,
while existing asynchronous router and preview callbacks used explicit pools.

The 15 interval GPU-allocation samples, in GB, were:

```text
1.404 1.404 1.628 1.790 1.877 2.413 2.699 3.097
3.782 4.131 4.691 4.915 5.003 5.040 5.189
```

First-to-last growth was 3.785 GB. The final record reported 450 render
completions at 30.025 FPS, five render drops, and zero decoded pixel host
copies.

## Minimal change

Commit: `d4ba6aa4661fe09d2cf95cf3d30a09cc4475d5aa`

Both public renderer `submit` overloads now execute their existing submission
body inside `@autoreleasepool`. The pool ends before the method returns; it does
not cover `wait_for_completion` or `drain`.

Release build and CTest passed 10/10 before the performance rerun. CMake was
then reconfigured at `d4ba6aa` so both final JSON records embed the exact tested
commit and `build_type=Release`.

## GREEN comparison

### Six-camera decode-render, 15 seconds

The 15 interval GPU-allocation samples, in GB, were:

```text
1.329 1.404 1.541 1.454 1.616 1.740 2.263 2.761
3.197 3.234 3.346 3.832 4.455 4.754 5.040
```

First-to-last growth was 3.711 GB, only 0.074 GB less than RED. The final record
reported 450 render completions at 30.016 FPS, five render drops, and zero
decoded pixel host copies. `OBJC_DEBUG_MISSING_POOLS` still emitted 542
diagnostics from other execution paths.

### Six-camera full, 15 seconds

Command used offscreen preview and hardware HEVC with the null sink. The GPU
samples, in GB, were:

```text
1.905 2.366 3.225 3.735 4.333 4.794 5.055 5.230
5.466 5.491 5.466 5.404 5.354 5.367 5.404
```

The last seven samples formed a 5.35-5.49 GB plateau. The final record reported
450 render completions at 30.019 FPS, 450 preview completions at 29.524 FPS,
449 hardware-HEVC completions at 29.950 FPS, zero callback/drain errors, and
zero decoded pixel host copies.

## Verdict

Hypothesis 2 is **falsified for the decode-render growth**. The renderer-local
autorelease pool did not materially change the main allocation curve or
throughput. Per the single-variable instruction, no third repair was added.
The source patch remains an isolated, behavior-preserving missing-pool cleanup,
but it is not the GPU-growth fix.
