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
decoder rebuild. The capacities are source-constructor parameters, with 16/8
defaults for the production configuration.
Surface capacities below four are rejected by both the source and decoded
surface pool: the mailbox can retain three slots while the callback needs a
fourth slot for forward progress. The focused probe verifies that capacities
1, 2, and 3 all fail construction; ticket capacity remains valid from 1
through 64.

Decode submission returns a typed result that distinguishes a normal fixed-pool
drop from stale input and recoverable VideoToolbox failure. Synchronous VT
errors and asynchronous callback/output-wrapper failures are propagated to the
source's existing lane-local rebuild path; pool exhaustion remains a bounded
frame drop. Each successful submission enables asynchronous decompression and
temporal processing, and its ticket remains callback-owned after `noErr`.

Publication uses the callback presentation timestamp. Within a generation the
decoder rejects invalid or non-increasing PTS, assigns sequence numbers only
when publishing, and marks the first replacement-generation frame as a
discontinuity. Missing or unknown color-matrix attachments are rejected rather
than silently treated as BT.709.

Lane recovery invalidates the failed decoder generation, drains old callbacks,
and applies the `CameraHealthTracker` 250 ms through 5 s backoff. A valid
compressed sample resets the backoff. Errors are lane-local and bounded to 512
bytes. Format changes retain their format description across sample lifetime,
advance the decoder generation, drain the prior session, and rebuild only that
lane.

Source control flow uses explicit fatal/recoverable failure kinds and a normal
EOF result; it never classifies errors by message text. Unsupported media,
malformed compressed samples, missing required hardware, and invalid
capabilities fail immediately. AVAssetReader and VideoToolbox operational
errors reconnect with backoff even when no finite run duration was configured.
EOF is successful for an unbounded file run and fatal only when it arrives
before a finite configured duration.

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
compressed byte size, ready data, exactly one sample/access unit, and an H.264
format description. The decoder repeats the one-sample validation defensively.

The review-wave probe was written before the typed source snapshot API and
failed on the missing `Mp4VideoToolboxSource::decoder_stats()` member. Its
green contract now checks callback throughput, strictly increasing consumed
PTS, measured frame rate, the actual CVPixelBuffer BT.709 attachment and
IOSurface, and zero steady C++ hot-path allocations after warmup. Native VT/CV
calls are outside the C++ allocation scopes and remain covered by the existing
native-object metrics.

A final constructor regression was added before the four-surface minimum. Its
RED run failed with `decoded surface capacity below four was not rejected`;
the same focused check is green for capacities 1, 2, and 3 after the pool/source
validation change.

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
Ran 19 tests in 3.313s
OK
```

Single 4K stream:

```text
build/macos/metal_decode_probe \
  /Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K/20260629_172532_cam1.mp4 \
  --frames 120
cam1 3840x2160 measured_fps=29.97 hardware=true callbacks=124
minimum_callbacks=120 published=124 dropped=0 consumed=120 host_copies=0
```

The latest focused run delivered 124 callbacks/publications and also verified
16 fixed tickets, one session/callback wrapper, two texture wrappers per
publication, zero pool exhaustion, equal submitted/callback counts, and zero
decoder errors.

Six independent real-time lanes for 10 seconds:

```text
build/macos/metal_decode_probe ...cam1.mp4 --six --seconds 10
cam1..cam6: measured_fps=29.97, 298 callbacks and 298 published
frames per lane (dynamic 90% minimum=269), hardware=true, host_copies=0;
strict PTS/BT.709/IOSurface and post-warmup allocation checks passed
```

Fresh AddressSanitizer six-lane run after the final source change:

```text
ASAN_OPTIONS=halt_on_error=1 build/macos-asan/metal_decode_probe \
  ...cam1.mp4 --six --seconds 10
exit 0; 298-299 callbacks per lane; measured_fps=29.97;
no ASan diagnostic
```

Apple's ASan runtime rejects `detect_leaks=1` as unsupported on this platform.
The native macOS leak checker was therefore run separately and reported:

```text
Process metal_decode_probe: 0 leaks for 0 total leaked bytes.
```

`git diff --check` is clean, and the decoder/source files contain no
`CVPixelBufferLockBaseAddress`, pixel-plane base-address access, or host-copy
operation.
