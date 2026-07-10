# Exact-Canvas Hardware HEVC Output Design

Date: 2026-07-11
Status: approved direction, implementation pending

## 1. Decision

The macOS backend encodes the stitched `5002x2102` BGRA IOSurface directly as
hardware HEVC. The six source MP4 streams remain H.264 and continue through the
existing AVFoundation/VideoToolbox decode lanes. This decision changes only the
final compressed-output consumer.

The output encoder must not resize the stitched canvas, split it into multiple
streams, read pixels on the CPU, or fall back to software encoding.

## 2. Capability Evidence

The target Apple M5 machine was probed before implementation:

- hardware-required H.264 session creation fails at `5002x2102` with
  `kVTInvalidSessionErr (-12903)`;
- hardware H.264 succeeds at `4096x2160` and `3840x2160`, but fails at tested
  widths `5000`, `5002`, and `5008`;
- allowing fallback at `5002x2102` creates only a software path, which is
  forbidden;
- hardware-required HEVC sessions succeed at `5002x2102` and `5008x2112`;
- the exact-size HEVC session accepts real-time, no-reordering, Main AutoLevel
  properties, reports `UsingHardware=true`, prepares successfully, and encodes
  a real IOSurface-backed BGRA frame with a successful callback.

## 3. Alternatives

1. **Hardware HEVC at exact `5002x2102` — selected.** It preserves the existing
   projection and the one-surface GPU path while satisfying the hardware-only
   requirement.
2. **Hardware H.264 after resizing to at most 4096 pixels wide — rejected.** It
   changes the validated output geometry and adds another GPU pass.
3. **Software H.264 or multiple H.264 output streams — rejected.** Software
   encoding violates the throughput goal; split output changes the consumer
   contract and no longer produces one stitched stream.

## 4. Data Flow and Ownership

The renderer completion queue fans out each completed `MetalOutputLease` to the
preview mailbox and encoder. The encoder receives the same IOSurface-backed
BGRA `CVPixelBufferRef`; it never maps pixel memory on the CPU.

`MetalEncoder::offer(MetalOutputLease, CMTime)` is non-blocking. It acquires one
record from a fixed `EncoderInputGate`, stores the lease, PTS, and submission
sequence, then calls `VTCompressionSessionEncodeFrame`. If the gate is full or
VideoToolbox rejects the frame, `offer` releases immediately and reports a
counted drop. The callback owns the input record until VideoToolbox has finished
using the pixel buffer, so the renderer output slot and its shared renderer
lifetime anchor cannot be released early.

The initial gate capacity is two. All encoder input records and compressed-I/O
scratch storage are allocated at startup or warm-up. No application-owned
per-frame heap allocation is permitted on the renderer-to-encoder path.

## 5. VideoToolbox Contract

Create `VTCompressionSession` for exact dimensions `5002x2102` with:

- codec `kCMVideoCodecType_HEVC`;
- both `RequireHardwareAcceleratedVideoEncoder` and
  `EnableHardwareAcceleratedVideoEncoder` enabled in the encoder specification;
- `kVTCompressionPropertyKey_RealTime = true`;
- `kVTCompressionPropertyKey_AllowFrameReordering = false`;
- `kVTProfileLevel_HEVC_Main_AutoLevel`;
- expected frame rate `30000/1001` where the API accepts a rational value;
- average bit rate `60,000,000` bits/s;
- maximum keyframe interval `60` frames.

After preparation, the backend must query
`kVTCompressionPropertyKey_UsingHardwareAcceleratedVideoEncoder` and fail
startup unless it is true. It must also fail startup if the session cannot
encode the exact canvas. There is no software or resize fallback.

Frame PTS is derived exactly as `sequence * 1001 / 30000` seconds. B-frames are
disabled, so callbacks must preserve monotonic decode and presentation order.

## 6. Annex-B HEVC Output

VideoToolbox returns length-prefixed HEVC access units. The callback converts
each NAL length prefix to a four-byte Annex-B start code without copying the
payload through a per-frame growable buffer. It must support non-contiguous
`CMBlockBuffer` storage with fixed scratch space and reject truncated or
zero-length NAL units.

For every keyframe/IRAP access unit, the writer emits the format description's
VPS, SPS, and PPS before the coded slices. The file sink uses `.h265` (or
`.hevc`) and the null sink validates the encoder while discarding compressed
bytes after accounting them.

Compressed file I/O is confined to the encoder callback path and must never
block the render coordinator. If the chosen file writer cannot accept data
without violating the bounded contract, the access unit is dropped and
counted; no unbounded queue may be introduced.

## 7. Metrics, Errors, and Shutdown

Report encoder submissions, completions, dropped offers, rejected frames,
callback errors, encoded bytes, completion FPS, input-gate occupancy and high
water, pool misses, and drain timeout. Hardware verification and the selected
codec/dimensions are included in final structured metrics.

Encoder saturation is recoverable. Session creation/preparation failure,
software-only operation, malformed VideoToolbox output, and inability to
create fixed resources are explicit errors.

Shutdown stops new renderer fan-out, drains renderer completions, flushes the
completion router, closes preview admission, calls
`VTCompressionSessionCompleteFrames`, waits for the bounded encoder callback
gate, then closes the writer and destroys the session. A bounded timeout must
preserve callback-owned state safely and record incomplete drain rather than
freeing callback context early.

## 8. Verification

Automated tests cover fixed-gate saturation and reuse, multi-NAL conversion,
non-contiguous block handling, parameter-set insertion, truncated/zero-length
rejection, callback-owned lease lifetime, hardware-only validation, and bounded
drain behavior.

The five-second acceptance run must produce a decodable Annex-B stream for
which `ffprobe -f hevc` reports `codec_name=hevc`, `width=5002`, and
`height=2102`. A 30-second preview-plus-encode run must remain near the target
rate, retain fixed pool bounds, report zero decoded-pixel host copies, and turn
consumer pressure into counted drops rather than render blocking.
