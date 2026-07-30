#include <swim/cudagl/nvdec_source.hpp>

#include <swim/core/camera_capacity.hpp>
#include <swim/core/camera_health.hpp>

extern "C" {
#include <libavcodec/avcodec.h>
#include <libavformat/avformat.h>
#include <libavutil/hwcontext.h>
#include <libavutil/hwcontext_cuda.h>
#include <libavutil/pixdesc.h>
}

#include <atomic>
#include <chrono>
#include <cstdio>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>

namespace swim::cudagl {
namespace {

constexpr std::size_t kMaximumLaneErrorBytes = 512;
// Frame geometry follows the stream (4K pool mp4, 720p underwater TS); only the
// NV12 half-resolution chroma constraint is universal.
constexpr std::uint32_t kMaxFrameDimension = 8192;

enum class LaneFailureKind : std::uint8_t { fatal, recoverable };

class LaneFailure final : public std::runtime_error {
 public:
  LaneFailure(LaneFailureKind kind, std::string message)
      : std::runtime_error(std::move(message)), kind_(kind) {}
  LaneFailureKind kind() const noexcept { return kind_; }

 private:
  LaneFailureKind kind_;
};

std::string av_err(const char* op, int code) {
  char buf[AV_ERROR_MAX_STRING_SIZE]{};
  av_strerror(code, buf, sizeof(buf));
  return std::string(op) + " failed: " + buf;
}

swim::core::ColorMatrix matrix_from_av(AVColorSpace space) noexcept {
  switch (space) {
    case AVCOL_SPC_BT470BG:
    case AVCOL_SPC_SMPTE170M:
      return swim::core::ColorMatrix::bt601;
    case AVCOL_SPC_BT2020_NCL:
    case AVCOL_SPC_BT2020_CL:
      return swim::core::ColorMatrix::bt2020;
    default:
      return swim::core::ColorMatrix::bt709;
  }
}

}  // namespace

// Owns one decoded AVFrame (AV_PIX_FMT_CUDA). The published FrameLease points
// at the CudaGlDecodedFrame embedded here; refcount reaching zero unrefs the
// AVFrame and frees the holder, releasing the CUDA surface back to the decoder.
struct DecodedFrameHolder final {
  CudaGlDecodedFrame frame;  // MUST be first: lease native ptr aliases this
  AVFrame* av_frame = nullptr;
  std::atomic_uint32_t references{0};
};

namespace {

void retain_holder(void* native) noexcept {
  auto* holder = static_cast<DecodedFrameHolder*>(native);
  if (holder == nullptr ||
      holder->references.fetch_add(1, std::memory_order_relaxed) == 0) {
    std::terminate();
  }
}

void release_holder(void* native) noexcept {
  auto* holder = static_cast<DecodedFrameHolder*>(native);
  if (holder == nullptr) {
    return;
  }
  if (holder->references.fetch_sub(1, std::memory_order_release) == 1) {
    if (holder->av_frame != nullptr) {
      av_frame_free(&holder->av_frame);
    }
    delete holder;
  }
}

}  // namespace

class NvdecSource::Impl final {
 public:
  Impl(std::shared_ptr<CudaGlContext> context, swim::core::SourceConfig source,
       std::uint32_t camera_index, swim::core::LatestFrameMailbox& mailbox,
       swim::core::RuntimeCounters& counters, swim::core::RunMode mode,
       std::uint32_t surface_capacity, swim::core::RunLifecycle* lifecycle,
       bool loop_sources)
      : context_(std::move(context)),
        source_(std::move(source)),
        camera_index_(camera_index),
        mailbox_(mailbox),
        counters_(counters),
        mode_(mode),
        surface_capacity_(surface_capacity),
        lifecycle_(lifecycle),
        loop_sources_(loop_sources) {
    if (source_.path.empty()) {
      throw std::invalid_argument("NVDEC source path must not be empty");
    }
    if (camera_index_ >= swim::core::kMaxCameras) {
      throw std::invalid_argument(
          "NVDEC source camera index must be below kMaxCameras");
    }
  }

  ~Impl() {
    stop();
    wait();
  }

  void start() {
    std::lock_guard lock(thread_mutex_);
    if (worker_.joinable() || running_.load(std::memory_order_acquire)) {
      throw std::logic_error("NVDEC source is already started");
    }
    stop_requested_.store(false, std::memory_order_release);
    failed_.store(false, std::memory_order_release);
    {
      std::lock_guard error_lock(error_mutex_);
      last_error_.clear();
    }
    worker_ = std::thread([this] { run(); });
  }

  void stop() noexcept {
    stop_requested_.store(true, std::memory_order_release);
  }

  void wait() {
    std::thread worker;
    {
      std::lock_guard lock(thread_mutex_);
      if (!worker_.joinable()) {
        return;
      }
      worker = std::move(worker_);
    }
    worker.join();
  }

  bool running() const noexcept {
    return running_.load(std::memory_order_acquire);
  }
  bool failed() const noexcept { return failed_.load(std::memory_order_acquire); }
  std::string last_error() const {
    std::lock_guard lock(error_mutex_);
    return last_error_;
  }

 private:
  void set_error(std::string message, bool fatal) noexcept {
    try {
      if (message.size() > kMaximumLaneErrorBytes) {
        message.resize(kMaximumLaneErrorBytes);
      }
      std::lock_guard lock(error_mutex_);
      last_error_ = std::move(message);
    } catch (...) {
    }
    if (fatal) {
      failed_.store(true, std::memory_order_release);
    }
  }

  bool termination_requested(
      std::chrono::steady_clock::time_point now) const noexcept {
    return stop_requested_.load(std::memory_order_acquire) ||
           (lifecycle_ != nullptr && lifecycle_->should_stop(now));
  }

  // Rewind the container to the start and flush the decoder for looping
  // playback. Returns false when the seek fails, letting the caller fall
  // through to normal EOF handling instead of spinning.
  bool rewind_stream(AVFormatContext* fmt, AVCodecContext* dec,
                     int video_stream) const noexcept {
    if (av_seek_frame(fmt, video_stream, 0, AVSEEK_FLAG_BACKWARD) < 0) {
      return false;
    }
    // The decoder just returned EOF, so it is drained; flushing clears that
    // state and lets it accept packets from the rewound position.
    avcodec_flush_buffers(dec);
    return true;
  }

  // True once `pts` reaches the lane's aligned start, measured from the clip's
  // own first frame in the stream's time base.
  bool past_start_offset(std::int64_t pts, AVRational tb,
                         std::int64_t& first_sample_pts) const noexcept {
    if (source_.start_offset.count() <= 0 || pts == AV_NOPTS_VALUE) {
      return true;
    }
    if (first_sample_pts == AV_NOPTS_VALUE) {
      first_sample_pts = pts;
    }
    const double seconds =
        static_cast<double>(pts - first_sample_pts) * av_q2d(tb);
    return seconds >=
           std::chrono::duration<double>(source_.start_offset).count();
  }

  void pace(std::int64_t pts, AVRational tb, std::int64_t& first_pts,
            std::chrono::steady_clock::time_point& first_wall) const {
    if (mode_ != swim::core::RunMode::realtime || pts == AV_NOPTS_VALUE) {
      return;
    }
    if (first_pts == AV_NOPTS_VALUE) {
      first_pts = pts;
      first_wall = std::chrono::steady_clock::now();
      return;
    }
    const double seconds = static_cast<double>(pts - first_pts) * av_q2d(tb);
    if (seconds <= 0.0) {
      return;
    }
    const auto target = first_wall + std::chrono::duration_cast<
        std::chrono::steady_clock::duration>(
        std::chrono::duration<double>{seconds});
    while (!termination_requested(std::chrono::steady_clock::now()) &&
           std::chrono::steady_clock::now() < target) {
      std::this_thread::sleep_until(
          std::min(target, std::chrono::steady_clock::now() +
                               std::chrono::milliseconds{5}));
    }
  }

  void publish_frame(AVFrame* hw_frame, std::uint64_t sequence) {
    // AV_PIX_FMT_CUDA: data[0]/data[1] are CUdeviceptr for luma/chroma planes,
    // linesize[] their pitch. Adopt the frame into a holder and hand its CUDA
    // pointers to the renderer without any host copy.
    auto* holder = new DecodedFrameHolder();
    holder->av_frame = av_frame_clone(hw_frame);
    if (holder->av_frame == nullptr) {
      delete holder;
      throw LaneFailure{LaneFailureKind::recoverable, "cannot clone CUDA frame"};
    }
    holder->references.store(1, std::memory_order_relaxed);
    auto& f = holder->frame;
    f.luma_ptr = reinterpret_cast<unsigned long long>(holder->av_frame->data[0]);
    f.chroma_ptr =
        reinterpret_cast<unsigned long long>(holder->av_frame->data[1]);
    f.luma_pitch = static_cast<std::size_t>(holder->av_frame->linesize[0]);
    f.chroma_pitch = static_cast<std::size_t>(holder->av_frame->linesize[1]);
    f.width = static_cast<std::uint32_t>(holder->av_frame->width);
    f.height = static_cast<std::uint32_t>(holder->av_frame->height);
    // NV12 chroma is half-resolution, so odd dimensions cannot be wrapped.
    if (f.width == 0 || f.height == 0 || (f.width & 1U) != 0 ||
        (f.height & 1U) != 0 || f.width > kMaxFrameDimension ||
        f.height > kMaxFrameDimension) {
      delete holder;
      throw LaneFailure{
          LaneFailureKind::fatal,
          "decoded frame dimensions must be even and within " +
              std::to_string(kMaxFrameDimension) + " (" +
              std::to_string(f.width) + "x" + std::to_string(f.height) + ")"};
    }
    f.owner = holder;
    f.metadata.camera_index = camera_index_;
    f.metadata.width = f.width;
    f.metadata.height = f.height;
    f.metadata.sequence = sequence;
    f.metadata.pixel_format =
        holder->av_frame->color_range == AVCOL_RANGE_JPEG
            ? swim::core::PixelFormat::nv12_full_range
            : swim::core::PixelFormat::nv12_video_range;
    f.metadata.color_matrix = matrix_from_av(holder->av_frame->colorspace);
    f.metadata.arrived_at = std::chrono::steady_clock::now();
    f.metadata.decoded_at = f.metadata.arrived_at;

    swim::core::FrameLease lease{
        holder,
        {retain_holder, release_holder, kCudaGlDecodedSurfaceTag},
        f.metadata};
    // holder starts at refcount 1; the lease adopts that reference.
    counters_.decoded.fetch_add(1, std::memory_order_relaxed);
    counters_.camera_decoded[camera_index_].fetch_add(
        1, std::memory_order_relaxed);
    counters_.published.fetch_add(1, std::memory_order_relaxed);
    counters_.camera_published[camera_index_].fetch_add(
        1, std::memory_order_relaxed);
    mailbox_.publish(std::move(lease));
  }

  void run_reader_once() {
    AVFormatContext* fmt = nullptr;
    int ret = avformat_open_input(&fmt, source_.path.string().c_str(), nullptr,
                                  nullptr);
    if (ret < 0) {
      throw LaneFailure{LaneFailureKind::recoverable,
                        av_err("avformat_open_input", ret)};
    }
    struct FmtGuard {
      AVFormatContext* f;
      ~FmtGuard() { if (f) avformat_close_input(&f); }
    } fmt_guard{fmt};

    if (avformat_find_stream_info(fmt, nullptr) < 0) {
      throw LaneFailure{LaneFailureKind::recoverable,
                        "avformat_find_stream_info failed"};
    }
    int video_stream = av_find_best_stream(fmt, AVMEDIA_TYPE_VIDEO, -1, -1,
                                           nullptr, 0);
    if (video_stream < 0) {
      throw LaneFailure{LaneFailureKind::fatal, "no video stream"};
    }
    AVStream* stream = fmt->streams[video_stream];
    if (stream->codecpar->codec_id != AV_CODEC_ID_H264) {
      throw LaneFailure{LaneFailureKind::fatal, "video stream is not H.264"};
    }

    // Prefer the dedicated CUVID decoder; it decodes straight to CUDA memory.
    const AVCodec* codec = avcodec_find_decoder_by_name("h264_cuvid");
    if (codec == nullptr) {
      throw LaneFailure{LaneFailureKind::fatal, "h264_cuvid decoder missing"};
    }
    AVCodecContext* dec = avcodec_alloc_context3(codec);
    if (dec == nullptr) {
      throw LaneFailure{LaneFailureKind::fatal, "cannot alloc decoder context"};
    }
    struct DecGuard {
      AVCodecContext* d;
      ~DecGuard() { if (d) avcodec_free_context(&d); }
    } dec_guard{dec};
    avcodec_parameters_to_context(dec, stream->codecpar);

    // Bind a CUDA hw device context so output stays as AV_PIX_FMT_CUDA.
    AVBufferRef* hw_device = nullptr;
    char device_index[16];
    std::snprintf(device_index, sizeof(device_index), "%d",
                  context_->cuda_device);
    ret = av_hwdevice_ctx_create(&hw_device, AV_HWDEVICE_TYPE_CUDA,
                                 device_index, nullptr, 0);
    if (ret < 0) {
      throw LaneFailure{LaneFailureKind::fatal,
                        av_err("av_hwdevice_ctx_create(CUDA)", ret)};
    }
    dec->hw_device_ctx = av_buffer_ref(hw_device);
    av_buffer_unref(&hw_device);

    ret = avcodec_open2(dec, codec, nullptr);
    if (ret < 0) {
      throw LaneFailure{LaneFailureKind::fatal, av_err("avcodec_open2", ret)};
    }

    AVPacket* packet = av_packet_alloc();
    AVFrame* frame = av_frame_alloc();
    struct PktFrameGuard {
      AVPacket* p;
      AVFrame* f;
      ~PktFrameGuard() {
        if (p) av_packet_free(&p);
        if (f) av_frame_free(&f);
      }
    } pf_guard{packet, frame};

    std::int64_t first_pts = AV_NOPTS_VALUE;
    std::int64_t first_sample_pts = AV_NOPTS_VALUE;
    std::chrono::steady_clock::time_point first_wall{};
    std::uint64_t sequence = 0;
    const AVRational tb = stream->time_base;

    while (!termination_requested(std::chrono::steady_clock::now())) {
      // Set when this iteration hit EOF and rewound instead of finishing, so
      // the end-of-iteration EOF check below does not end the lane.
      bool looped = false;
      ret = av_read_frame(fmt, packet);
      if (ret == AVERROR_EOF) {
        avcodec_send_packet(dec, nullptr);  // flush
      } else if (ret < 0) {
        throw LaneFailure{LaneFailureKind::recoverable,
                          av_err("av_read_frame", ret)};
      } else if (packet->stream_index != video_stream) {
        av_packet_unref(packet);
        continue;
      } else {
        counters_.received.fetch_add(1, std::memory_order_relaxed);
        counters_.camera_received[camera_index_].fetch_add(
            1, std::memory_order_relaxed);
        if (avcodec_send_packet(dec, packet) < 0) {
          av_packet_unref(packet);
          continue;
        }
        av_packet_unref(packet);
      }

      for (;;) {
        int recv = avcodec_receive_frame(dec, frame);
        if (recv == AVERROR(EAGAIN)) {
          break;
        }
        if (recv == AVERROR_EOF) {
          // Looping playback: rewind and keep going. Mirrors mf_source.cpp —
          // everything derived from sample timestamps is reset so the lane
          // replays its manifest start offset and re-anchors pacing.
          if (loop_sources_ && rewind_stream(fmt, dec, video_stream)) {
            first_pts = AV_NOPTS_VALUE;
            first_sample_pts = AV_NOPTS_VALUE;
            first_wall = {};
            counters_.reconnects.fetch_add(1, std::memory_order_relaxed);
            looped = true;
            break;
          }
          const auto now = std::chrono::steady_clock::now();
          if (lifecycle_ != nullptr) {
            const auto disposition = lifecycle_->classify_eof(now);
            if (disposition ==
                    swim::core::SourceEofDisposition::normal_after_stop ||
                disposition ==
                    swim::core::SourceEofDisposition::normal_after_deadline) {
              return;
            }
            throw LaneFailure{
                LaneFailureKind::fatal,
                swim::core::source_eof_failure_message(disposition)};
          }
          return;
        }
        if (recv < 0) {
          throw LaneFailure{LaneFailureKind::recoverable,
                            av_err("avcodec_receive_frame", recv)};
        }
        if (frame->format != AV_PIX_FMT_CUDA) {
          av_frame_unref(frame);
          counters_.decoded_pixel_host_copies.fetch_add(
              1, std::memory_order_relaxed);
          throw LaneFailure{LaneFailureKind::fatal,
                            "decoder did not produce CUDA frames"};
        }
        // Skip forward to this lane's aligned start; see mf_source.cpp.
        if (!past_start_offset(frame->pts, tb, first_sample_pts)) {
          av_frame_unref(frame);
          continue;
        }
        pace(frame->pts, tb, first_pts, first_wall);
        publish_frame(frame, sequence++);
        av_frame_unref(frame);
      }
      if (ret == AVERROR_EOF && !looped) {
        return;
      }
    }
  }

  void run() noexcept {
    running_.store(true, std::memory_order_release);
    try {
      swim::core::CameraHealthTracker health;
      while (!termination_requested(std::chrono::steady_clock::now())) {
        try {
          run_reader_once();
          break;
        } catch (const LaneFailure& error) {
          set_error(error.what(), false);
          if (error.kind() == LaneFailureKind::fatal) {
            throw;
          }
          counters_.reconnects.fetch_add(1, std::memory_order_relaxed);
          const auto until =
              std::chrono::steady_clock::now() + health.next_reconnect_delay();
          while (!termination_requested(std::chrono::steady_clock::now()) &&
                 std::chrono::steady_clock::now() < until) {
            std::this_thread::sleep_for(std::chrono::milliseconds{10});
          }
          if (termination_requested(std::chrono::steady_clock::now())) {
            break;
          }
        }
      }
    } catch (const std::exception& error) {
      set_error(error.what(), true);
    } catch (...) {
      set_error("unknown NVDEC lane failure", true);
    }
    running_.store(false, std::memory_order_release);
  }

  std::shared_ptr<CudaGlContext> context_;
  swim::core::SourceConfig source_;
  std::uint32_t camera_index_{};
  swim::core::LatestFrameMailbox& mailbox_;
  swim::core::RuntimeCounters& counters_;
  swim::core::RunMode mode_;
  std::uint32_t surface_capacity_{};
  swim::core::RunLifecycle* lifecycle_{};
  bool loop_sources_{false};
  mutable std::mutex thread_mutex_;
  std::thread worker_;
  std::atomic_bool stop_requested_{false};
  std::atomic_bool running_{false};
  std::atomic_bool failed_{false};
  mutable std::mutex error_mutex_;
  std::string last_error_;
};

NvdecSource::NvdecSource(std::shared_ptr<CudaGlContext> context,
                         swim::core::SourceConfig source,
                         std::uint32_t camera_index,
                         swim::core::LatestFrameMailbox& mailbox,
                         swim::core::RuntimeCounters& counters,
                         swim::core::RunMode mode,
                         std::uint32_t surface_capacity,
                         swim::core::RunLifecycle* lifecycle,
                         bool loop_sources)
    : impl_(std::make_unique<Impl>(std::move(context), std::move(source),
                                   camera_index, mailbox, counters, mode,
                                   surface_capacity, lifecycle,
                                   loop_sources)) {}

NvdecSource::~NvdecSource() = default;

void NvdecSource::start() { impl_->start(); }
void NvdecSource::stop() noexcept { impl_->stop(); }
void NvdecSource::wait() { impl_->wait(); }
bool NvdecSource::running() const noexcept { return impl_->running(); }
bool NvdecSource::failed() const noexcept { return impl_->failed(); }
std::string NvdecSource::last_error() const { return impl_->last_error(); }

}  // namespace swim::cudagl

