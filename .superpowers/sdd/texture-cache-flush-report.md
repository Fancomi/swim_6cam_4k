# Metal texture-cache flush hypothesis report

## Scope

This was the third and final single-variable investigation of the observed
Metal allocation growth. The hypothesis was:

> Decoded `CVMetalTexture` wrappers remain cached after their frame leases are
> released, so flushing the shared `CVMetalTextureCache` at the existing 1 Hz
> telemetry boundary will release unused cache entries and stop the growth.

The experiment added only this call immediately before reading
`MTLDevice.currentAllocatedSize` in `MetalBackend::sample_runtime()`:

```objective-c++
CVMetalTextureCacheFlush(context_->texture_cache, 0);
```

It did not change decoder or renderer hot paths, pools, capacities, waits,
submission behavior, or callback ownership. The decoder and renderer
autorelease-pool correctness changes from the first two hypotheses remained in
place.

## RED evidence

The immediately preceding renderer-autorelease experiment at commit
`d4ba6aa4661fe09d2cf95cf3d30a09cc4475d5aa` still grew from 1.329 to 5.040 GB
in the 15-second six-camera decode-render run. Its full run plateaued near
5.4 GB, but the decode-render growth remained unresolved.

## Minimal experiment

Experiment commit: `d97a2c452e126643a0e86393b7dbf8771830c7d0`

The Release build succeeded and CTest passed 10/10 before measurement. CMake
was reconfigured after the experiment commit; both final JSON records reported
the exact experiment SHA and `build_type=Release`.

### Six-camera decode-render, 15 seconds

Command:

```bash
build/metal-release/swim_realtime \
  --config configs/macos_20260629.conf \
  --stage=decode-render --stream-count=6 --mode=realtime \
  --duration-seconds=15 --preview=false --encode=false \
  --metrics=/tmp/cache-flush-decode-render.jsonl
```

The 15 interval Metal allocation samples, in GB, were:

```text
1.628 1.728 1.989 2.176 2.537 3.247 3.807 4.392
4.841 5.015 5.177 5.227 5.351 5.351 5.364
```

First-to-last growth was 3.736 GB and the maximum was 5.364 GB. The final
record reported 450 render completions at 30.022 FPS, five render drops, and
zero decoded pixel host copies.

### Six-camera full, 15 seconds

Command:

```bash
build/metal-release/swim_realtime \
  --config configs/macos_20260629.conf \
  --stage=full --stream-count=6 --mode=realtime \
  --duration-seconds=15 --preview=true --preview-visible=false \
  --encode=true --encode-sink=null \
  --metrics=/tmp/cache-flush-full.jsonl
```

The interval Metal allocation samples, in GB, were:

```text
1.768 1.756 1.930 2.167 2.291 2.403 2.577 2.752
3.001 3.088 3.387 3.574 3.910 4.121 4.669
```

First-to-last growth was 2.901 GB and the final samples were still rising. The
final record reported 448 render completions at 29.885 FPS, 448 offscreen
preview completions at 29.419 FPS, 446 hardware-HEVC completions at 29.993 FPS,
five render drops, and zero decoded pixel host copies.

## Verdict and disposition

Hypothesis 3 is **falsified**. Flushing the shared texture cache once per
second did not keep the observed Metal allocation below 3 GB and did not stop
the rising curve. A 30-second confirmation run was intentionally not performed
because the predeclared 15-second failure criterion had already been met.

The experiment was reverted by commit
`8eb96f79d1d6db032c2e4927549e442d84bb4858` because it added recurring work
without solving the problem. The experiment and revert remain in history as
evidence. Per the three-hypothesis limit, this investigation stops here; no
fourth speculative repair was attempted.
