### Task 8 Report: Native MP4 Demux and VideoToolbox Decode Lanes

#### Result

Implemented one independent `Mp4VideoToolboxSource`/`VideoToolboxDecoder` lane
per camera. The source reads compressed H.264 access units with
`AVAssetReaderTrackOutput(outputSettings:nil)` and
`alwaysCopiesSampleData = NO`. The decoder requires and queries hardware
VideoToolbox decode, requests IOSurface-backed Metal-compatible video-range
NV12, creates each accepted frame's luma/chroma wrappers once in the callback,
and publishes an intrusive `MetalDecodedSurface` lease directly to the lane's
latest-frame mailbox.

The hot path uses fixed pools of 16 decode tickets and 8 decoded surfaces. It
does not lock a pixel buffer, copy decoded pixels to host memory, or allocate a
replacement callback wrapper per frame. Active decoded leases anchor their
surface pool and shared `MetalContext`, so mailbox leases can safely outlive a
decoder rebuild.

Lane recovery invalidates the failed decoder generation, drains old callbacks,
and applies the `CameraHealthTracker` 250 ms through 5 s backoff. A valid
compressed sample resets the backoff. Errors are lane-local and bounded to 512
bytes. Format changes retain their format description across sample lifetime,
advance the decoder generation, drain the prior session, and rebuild only that
lane.

#### TDD evidence

The first probe and CMake target were added before production files. The RED
build failed with:

```text
fatal error: 'swim/metal/mp4_source.hpp' file not found
ninja: build stopped: subcommand failed.
```

The real data exposed an initial ready timing-only `CMSampleBuffer` marker
(`samples=0`, `bytes=0`). The source now skips only that non-frame marker and
requires every submitted access unit to have no image buffer, positive
compressed byte size, ready data, and an H.264 format description.

#### Verification

Normal full build and C++ tests:

```text
cmake --build build/macos
ctest --test-dir build/macos --output-on-failure
100% tests passed, 0 tests failed out of 7
```

Python regression suite:

```text
.venv/bin/python -m unittest discover -s python/tests -v
Ran 19 tests in 3.220s
OK
```

Single 4K stream:

```text
build/macos/metal_decode_probe \
  /Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K/20260629_172532_cam1.mp4 \
  --frames 120
cam1 3840x2160 30000/1001 hardware=true callbacks=125 published=125 consumed=121 host_copies=0
```

Six independent real-time lanes for 10 seconds:

```text
build/macos/metal_decode_probe ...cam1.mp4 --six --seconds 10
cam1..cam6: 298 callbacks and 298 published frames per lane,
hardware=true, host_copies=0
```

Fresh AddressSanitizer six-lane run after the final source change:

```text
ASAN_OPTIONS=halt_on_error=1 build/macos-asan/metal_decode_probe \
  ...cam1.mp4 --six --seconds 10
exit 0; 294-298 callbacks per lane; no ASan diagnostic
```

Apple's ASan runtime rejects `detect_leaks=1` as unsupported on this platform.
The native macOS leak checker was therefore run separately and reported:

```text
Process metal_decode_probe: 0 leaks for 0 total leaked bytes.
```

`git diff --check` is clean, and the decoder/source files contain no
`CVPixelBufferLockBaseAddress`, pixel-plane base-address access, or host-copy
operation.
