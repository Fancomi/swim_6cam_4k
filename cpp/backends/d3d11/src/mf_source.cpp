#include <swim/d3d11/mf_source.hpp>

#include <swim/core/camera_capacity.hpp>
#include <swim/core/camera_health.hpp>

#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <wrl/client.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <exception>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace swim::d3d11 {
namespace {

using Microsoft::WRL::ComPtr;

constexpr std::size_t kMaximumLaneErrorBytes = 512;
// Frame geometry follows the stream: 3840x2160 for the pool rig, 1280x720 for
// the underwater rig. NV12 chroma is half-resolution, so only even dimensions
// can be wrapped as shader resource views.
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

std::string hr_error(const char* operation, HRESULT hr) {
  char buffer[32];
  std::snprintf(buffer, sizeof(buffer), "0x%08lx", static_cast<unsigned long>(hr));
  return std::string(operation) + " failed (HRESULT " + buffer + ")";
}

// One-time Media Foundation startup/shutdown for the whole process. Lanes take
// a shared reference; MF is torn down when the last lane releases it.
class MediaFoundationRuntime final {
 public:
  MediaFoundationRuntime() {
    const auto hr = MFStartup(MF_VERSION, MFSTARTUP_LITE);
    if (FAILED(hr)) {
      throw std::runtime_error(hr_error("MFStartup", hr));
    }
  }
  ~MediaFoundationRuntime() { MFShutdown(); }
  MediaFoundationRuntime(const MediaFoundationRuntime&) = delete;
  MediaFoundationRuntime& operator=(const MediaFoundationRuntime&) = delete;
};

std::shared_ptr<MediaFoundationRuntime> media_foundation_runtime() {
  static std::mutex mutex;
  static std::weak_ptr<MediaFoundationRuntime> weak;
  std::lock_guard lock(mutex);
  if (auto existing = weak.lock()) {
    return existing;
  }
  auto runtime = std::make_shared<MediaFoundationRuntime>();
  weak = runtime;
  return runtime;
}

swim::core::ColorMatrix matrix_from_mf(UINT32 value) noexcept {
  switch (value) {
    case MFVideoTransferMatrix_BT601:
      return swim::core::ColorMatrix::bt601;
    case MFVideoTransferMatrix_BT2020_10:
    case MFVideoTransferMatrix_BT2020_12:
      return swim::core::ColorMatrix::bt2020;
    default:
      return swim::core::ColorMatrix::bt709;
  }
}

}  // namespace

// A lane-local pool of NV12 shader-readable textures. Media Foundation decoder
// output textures are bound with D3D11_BIND_DECODER and are not directly
// shader-readable, so each decoded frame is copied (device-to-device, no host
// staging) into one of these slots, which expose luma (R8) and chroma (R8G8)
// SRVs. Slots are reference counted; a slot returns to the pool only after the
// renderer's GPU use of it completes and the FrameLease is released.
class DecodedSurfacePool final
    : public std::enable_shared_from_this<DecodedSurfacePool> {
 public:
  struct Surface final {
    // view MUST be the first member: the published FrameLease carries a
    // Surface* as its native pointer, and the renderer reinterprets it as a
    // D3D11FrameView*. Keeping view first makes those two addresses identical.
    D3D11FrameView view;
    ComPtr<ID3D11Texture2D> texture;
    ComPtr<ID3D11ShaderResourceView> luma_srv;
    ComPtr<ID3D11ShaderResourceView> chroma_srv;
    std::atomic_uint32_t references{0};
    std::uint32_t pool_index = 0;
    DecodedSurfacePool* owner = nullptr;
    // Keeps the pool alive while this slot is leased, so the pool can outlive
    // its owning MfSource::Impl if the mailbox still holds published frames at
    // shutdown. Mirrors MetalDecodedSurface::lifetime_anchor.
    std::shared_ptr<DecodedSurfacePool> lifetime_anchor;
  };

  DecodedSurfacePool(std::shared_ptr<D3D11Context> context,
                     std::uint32_t capacity, std::uint32_t width,
                     std::uint32_t height, std::uint32_t camera_index)
      : context_(std::move(context)),
        capacity_(capacity),
        slots_(std::make_unique<Surface[]>(capacity)) {
    D3D11_TEXTURE2D_DESC desc{};
    desc.Width = width;
    desc.Height = height;
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.Format = DXGI_FORMAT_NV12;
    desc.SampleDesc.Count = 1;
    desc.Usage = D3D11_USAGE_DEFAULT;
    desc.BindFlags = D3D11_BIND_SHADER_RESOURCE;
    for (std::uint32_t index = 0; index < capacity_; ++index) {
      auto& slot = slots_[index];
      slot.pool_index = index;
      slot.owner = this;
      if (FAILED(context_->device->CreateTexture2D(
              &desc, nullptr, slot.texture.GetAddressOf()))) {
        throw std::runtime_error("cannot allocate D3D11 NV12 decode surface");
      }
      D3D11_SHADER_RESOURCE_VIEW_DESC luma{};
      luma.Format = DXGI_FORMAT_R8_UNORM;
      luma.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
      luma.Texture2D.MipLevels = 1;
      if (FAILED(context_->device->CreateShaderResourceView(
              slot.texture.Get(), &luma, slot.luma_srv.GetAddressOf()))) {
        throw std::runtime_error("cannot create NV12 luma view");
      }
      D3D11_SHADER_RESOURCE_VIEW_DESC chroma{};
      chroma.Format = DXGI_FORMAT_R8G8_UNORM;
      chroma.ViewDimension = D3D11_SRV_DIMENSION_TEXTURE2D;
      chroma.Texture2D.MipLevels = 1;
      if (FAILED(context_->device->CreateShaderResourceView(
              slot.texture.Get(), &chroma, slot.chroma_srv.GetAddressOf()))) {
        throw std::runtime_error("cannot create NV12 chroma view");
      }
      slot.view.luma = slot.luma_srv.Get();
      slot.view.chroma = slot.chroma_srv.Get();
      slot.view.metadata.camera_index = camera_index;
      slot.view.metadata.width = width;
      slot.view.metadata.height = height;
      slot.view.metadata.pixel_format =
          swim::core::PixelFormat::nv12_video_range;
    }
  }

  ~DecodedSurfacePool() noexcept {
    for (std::uint32_t index = 0; index < capacity_; ++index) {
      if (slots_[index].references.load(std::memory_order_acquire) != 0) {
        std::terminate();
      }
    }
  }

  Surface* try_acquire() noexcept {
    for (std::uint32_t index = 0; index < capacity_; ++index) {
      auto& slot = slots_[index];
      std::uint32_t expected = 0;
      if (slot.references.compare_exchange_strong(
              expected, 1, std::memory_order_acquire,
              std::memory_order_relaxed)) {
        in_use_.fetch_add(1, std::memory_order_relaxed);
        // Anchor the pool alive for as long as any slot is leased. Published
        // frames may outlive this pool's owner during shutdown.
        slot.lifetime_anchor = shared_from_this();
        return &slot;
      }
    }
    return nullptr;
  }

  void release(Surface* slot) noexcept {
    const auto previous =
        slot->references.fetch_sub(1, std::memory_order_release);
    if (previous == 0) {
      std::terminate();
    }
    if (previous == 1) {
      in_use_.fetch_sub(1, std::memory_order_relaxed);
      // Drop the anchor last; this may destroy the pool if it was the final
      // outstanding lease and the owner has already released its reference.
      slot->lifetime_anchor.reset();
    }
  }

  std::uint32_t capacity() const noexcept { return capacity_; }

 private:
  std::shared_ptr<D3D11Context> context_;
  std::uint32_t capacity_{};
  std::unique_ptr<Surface[]> slots_;
  std::atomic_uint32_t in_use_{0};
};

namespace {

void retain_decoded_surface(void* native) noexcept {
  auto* slot = static_cast<DecodedSurfacePool::Surface*>(native);
  if (slot == nullptr ||
      slot->references.fetch_add(1, std::memory_order_relaxed) == 0) {
    std::terminate();
  }
}

void release_decoded_surface(void* native) noexcept {
  auto* slot = static_cast<DecodedSurfacePool::Surface*>(native);
  if (slot != nullptr && slot->owner != nullptr) {
    slot->owner->release(slot);
  }
}

}  // namespace

class MfSource::Impl final {
 public:
  Impl(std::shared_ptr<D3D11Context> context, swim::core::SourceConfig source,
       std::uint32_t camera_index, swim::core::LatestFrameMailbox& mailbox,
       swim::core::RuntimeCounters& counters, swim::core::RunMode mode,
       std::uint32_t ticket_capacity, std::uint32_t surface_capacity,
       swim::core::RunLifecycle* lifecycle, bool loop_sources,
       bool stop_at_eof, std::chrono::milliseconds loop_period,
       swim::core::SharedLaneOrigin* shared_origin)
      : context_(std::move(context)),
        source_(std::move(source)),
        camera_index_(camera_index),
        mailbox_(mailbox),
        counters_(counters),
        mode_(mode),
        surface_capacity_(surface_capacity),
        lifecycle_(lifecycle),
        loop_sources_(loop_sources),
        stop_at_eof_(stop_at_eof),
        pacer_(mode, source_.start_offset, loop_sources, loop_period,
               shared_origin),
        mf_runtime_(media_foundation_runtime()) {
    static_cast<void>(ticket_capacity);
    if (context_ == nullptr || context_->device == nullptr) {
      throw std::invalid_argument("MF source requires a valid D3D11 context");
    }
    if (source_.path.empty()) {
      throw std::invalid_argument("MF source path must not be empty");
    }
    if (camera_index_ >= swim::core::kMaxCameras) {
      throw std::invalid_argument(
          "MF source camera index must be below kMaxCameras");
    }
    if (surface_capacity_ < 4 || surface_capacity_ > 64) {
      throw std::invalid_argument(
          "MF decoder surface capacity must be between 4 and 64");
    }
  }

  ~Impl() {
    stop();
    wait();
  }

  void start() {
    std::lock_guard lock(thread_mutex_);
    if (worker_.joinable() || running_.load(std::memory_order_acquire)) {
      throw std::logic_error("MF source is already started");
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

  bool failed() const noexcept {
    return failed_.load(std::memory_order_acquire);
  }

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

  std::chrono::steady_clock::time_point effective_deadline() const noexcept {
    return lifecycle_ == nullptr ? std::chrono::steady_clock::time_point::max()
                                 : lifecycle_->deadline();
  }

  bool termination_requested(
      std::chrono::steady_clock::time_point now) const noexcept {
    return stop_requested_.load(std::memory_order_acquire) ||
           (lifecycle_ != nullptr && lifecycle_->should_stop(now));
  }

  ComPtr<IMFSourceReader> create_reader() {
    // Bind the reader to the shared D3D11 device so decode output is a
    // GPU-resident ID3D11Texture2D (DXVA / D3D11VA hardware path).
    ComPtr<IMFDXGIDeviceManager> manager;
    UINT reset_token = 0;
    if (FAILED(MFCreateDXGIDeviceManager(&reset_token, manager.GetAddressOf()))) {
      throw LaneFailure{LaneFailureKind::fatal,
                        "cannot create MF DXGI device manager"};
    }
    if (FAILED(manager->ResetDevice(context_->device.Get(), reset_token))) {
      throw LaneFailure{LaneFailureKind::fatal,
                        "cannot bind D3D11 device to MF"};
    }

    ComPtr<IMFAttributes> attributes;
    if (FAILED(MFCreateAttributes(attributes.GetAddressOf(), 3))) {
      throw LaneFailure{LaneFailureKind::fatal,
                        "cannot create MF reader attributes"};
    }
    attributes->SetUnknown(MF_SOURCE_READER_D3D_MANAGER, manager.Get());
    attributes->SetUINT32(MF_SOURCE_READER_ENABLE_ADVANCED_VIDEO_PROCESSING,
                          TRUE);
    attributes->SetUINT32(MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, TRUE);

    const auto wide = source_.path.wstring();
    ComPtr<IMFSourceReader> reader;
    const auto hr = MFCreateSourceReaderFromURL(
        wide.c_str(), attributes.Get(), reader.GetAddressOf());
    if (FAILED(hr)) {
      throw LaneFailure{LaneFailureKind::recoverable,
                        hr_error("MFCreateSourceReaderFromURL", hr)};
    }

    // Force NV12 output and select only the first video stream.
    reader->SetStreamSelection(
        static_cast<DWORD>(MF_SOURCE_READER_ALL_STREAMS), FALSE);
    reader->SetStreamSelection(
        static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM), TRUE);
    ComPtr<IMFMediaType> output_type;
    MFCreateMediaType(output_type.GetAddressOf());
    output_type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    output_type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    const auto set_hr = reader->SetCurrentMediaType(
        static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM), nullptr,
        output_type.Get());
    if (FAILED(set_hr)) {
      throw LaneFailure{LaneFailureKind::fatal,
                        hr_error("SetCurrentMediaType(NV12)", set_hr)};
    }
    return reader;
  }

  void read_color_metadata(IMFSourceReader* reader) {
    ComPtr<IMFMediaType> current;
    if (FAILED(reader->GetCurrentMediaType(
            static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM),
            current.GetAddressOf()))) {
      return;
    }
    UINT32 width = 0;
    UINT32 height = 0;
    MFGetAttributeSize(current.Get(), MF_MT_FRAME_SIZE, &width, &height);
    if (width == 0 || height == 0 || (width & 1U) != 0 || (height & 1U) != 0 ||
        width > kMaxFrameDimension || height > kMaxFrameDimension) {
      throw LaneFailure{
          LaneFailureKind::fatal,
          "MP4 video dimensions must be even and within " +
              std::to_string(kMaxFrameDimension) + " (" +
              std::to_string(width) + "x" + std::to_string(height) + ")"};
    }
    UINT32 matrix = 0;
    if (SUCCEEDED(current->GetUINT32(MF_MT_YUV_MATRIX, &matrix))) {
      color_matrix_ = matrix_from_mf(matrix);
    }
    UINT32 range = 0;
    if (SUCCEEDED(current->GetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, &range))) {
      full_range_ = range == MFNominalRange_0_255;
    }
    frame_width_ = width;
    frame_height_ = height;
  }

  // Rewind the reader to the clip start for looping playback. Seeking to 0 and
  // re-decoding up to the aligned start is deliberate: a seek to the aligned
  // start lands on the nearest earlier keyframe, and those sit at GOP
  // granularity, so lanes would resume at different content points. Measured
  // 2026-07-31: seeking to the aligned start instead raised snapshot_age_spread
  // p99 from 88ms to 961ms here and 311ms to 970ms on cudagl. The re-decode is
  // paid as a brief late start, which is the cheaper error. Returns false when
  // the seek fails, letting the caller fall through to normal EOF handling
  // rather than spinning on a stream that will not restart.
  bool seek_to_start(IMFSourceReader* reader) const noexcept {
    PROPVARIANT position;
    PropVariantInit(&position);
    position.vt = VT_I8;
    position.hVal.QuadPart = 0;
    const auto hr = reader->SetCurrentPosition(GUID_NULL, position);
    PropVariantClear(&position);
    return SUCCEEDED(hr);
  }

  void publish_sample(IMFSample* sample, std::uint64_t sequence) {
    ComPtr<IMFMediaBuffer> buffer;
    // Do NOT use ConvertToContiguousBuffer: that would copy the GPU surface
    // into a system-memory buffer and lose the IMFDXGIBuffer interface. The
    // hardware decode path exposes the D3D11 texture through buffer index 0.
    if (FAILED(sample->GetBufferByIndex(0, buffer.GetAddressOf()))) {
      throw LaneFailure{LaneFailureKind::recoverable,
                        "cannot obtain MF sample buffer"};
    }
    ComPtr<IMFDXGIBuffer> dxgi_buffer;
    if (FAILED(buffer.As(&dxgi_buffer))) {
      counters_.decoded_pixel_host_copies.fetch_add(1,
                                                    std::memory_order_relaxed);
      throw LaneFailure{LaneFailureKind::fatal,
                        "MF sample is not a D3D11 surface (no hardware decode)"};
    }
    ComPtr<ID3D11Texture2D> decoded;
    if (FAILED(dxgi_buffer->GetResource(
            __uuidof(ID3D11Texture2D),
            reinterpret_cast<void**>(decoded.GetAddressOf())))) {
      throw LaneFailure{LaneFailureKind::recoverable,
                        "cannot obtain decoded D3D11 texture"};
    }
    UINT subresource = 0;
    dxgi_buffer->GetSubresourceIndex(&subresource);

    auto* slot = pool_->try_acquire();
    if (slot == nullptr) {
      // No shader-readable surface free; the renderer is behind. Drop this
      // decoded frame rather than block the decode lane.
      counters_.overwritten.fetch_add(1, std::memory_order_relaxed);
      counters_.camera_overwritten[camera_index_].fetch_add(
          1, std::memory_order_relaxed);
      return;
    }

    {
      // Device-to-device copy from the (decoder-bound) output into the
      // shader-readable NV12 slot. No host staging.
      std::lock_guard lock(context_->context_mutex);
      context_->immediate_context->CopySubresourceRegion(
          slot->texture.Get(), 0, 0, 0, 0, decoded.Get(), subresource,
          nullptr);
    }

    slot->view.metadata.camera_index = camera_index_;
    slot->view.metadata.width = frame_width_;
    slot->view.metadata.height = frame_height_;
    slot->view.metadata.sequence = sequence;
    slot->view.metadata.decoder_generation = 0;
    slot->view.metadata.pixel_format =
        full_range_ ? swim::core::PixelFormat::nv12_full_range
                    : swim::core::PixelFormat::nv12_video_range;
    slot->view.metadata.color_matrix = color_matrix_;
    slot->view.metadata.arrived_at = std::chrono::steady_clock::now();
    slot->view.metadata.decoded_at = slot->view.metadata.arrived_at;

    swim::core::FrameLease lease{
        slot,
        {retain_decoded_surface, release_decoded_surface,
         kD3D11DecodedSurfaceTag},
        slot->view.metadata};
    // try_acquire already left the slot at refcount 1; FrameLease adopts that
    // single reference (its constructor does not retain). Do not balance it
    // here or the lease's destructor would drive the count negative.
    counters_.decoded.fetch_add(1, std::memory_order_relaxed);
    counters_.camera_decoded[camera_index_].fetch_add(
        1, std::memory_order_relaxed);
    counters_.published.fetch_add(1, std::memory_order_relaxed);
    counters_.camera_published[camera_index_].fetch_add(
        1, std::memory_order_relaxed);
    mailbox_.publish(std::move(lease));
  }

  void run_reader_once() {
    auto reader = create_reader();
    read_color_metadata(reader.Get());
    pool_ = std::make_shared<DecodedSurfacePool>(
        context_, surface_capacity_, frame_width_, frame_height_,
        camera_index_);
    counters_.decode_surface_capacity[camera_index_].store(
        pool_->capacity(), std::memory_order_relaxed);

    // MF timestamps are in 100ns units; the pacer works in nanoseconds.
    const auto to_ns = [](LONGLONG pts_100ns) {
      return pts_100ns < 0 ? swim::core::LanePacer::kInvalidPts
                           : pts_100ns * 100;
    };
    const auto running = [this] {
      return !termination_requested(std::chrono::steady_clock::now());
    };
    pacer_.begin_run(std::chrono::steady_clock::now());
    std::uint64_t sequence = 0;
    while (running()) {
      DWORD stream_flags = 0;
      LONGLONG timestamp = 0;
      ComPtr<IMFSample> sample;
      const auto hr = reader->ReadSample(
          static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM), 0, nullptr,
          &stream_flags, &timestamp, sample.GetAddressOf());
      if (FAILED(hr)) {
        throw LaneFailure{LaneFailureKind::recoverable,
                          hr_error("IMFSourceReader::ReadSample", hr)};
      }
      if ((stream_flags & MF_SOURCE_READERF_ENDOFSTREAM) != 0) {
        const auto now = std::chrono::steady_clock::now();
        // Looping playback: rewind and keep publishing. advance_pass() clears
        // everything the pass derived from timestamps and moves the wall origin
        // on by one period, so the lane replays its manifest start offset and the
        // cadence continues instead of restarting.
        if (loop_sources_ && seek_to_start(reader.Get())) {
          pacer_.advance_pass();
          counters_.reconnects.fetch_add(1, std::memory_order_relaxed);
          continue;
        }
        if (stop_at_eof_) {
          // The clips are the whole run: finishing them is success, so end the
          // run instead of reporting this lane as failed. Mirrors
          // mp4_source.mm — the same config key must mean the same thing on
          // both backends.
          if (lifecycle_ != nullptr) {
            lifecycle_->request_stop();
          }
          return;
        }
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
      if ((stream_flags & MF_SOURCE_READERF_CURRENTMEDIATYPECHANGED) != 0) {
        read_color_metadata(reader.Get());
      }
      if (sample == nullptr) {
        continue;
      }
      counters_.received.fetch_add(1, std::memory_order_relaxed);
      counters_.camera_received[camera_index_].fetch_add(
          1, std::memory_order_relaxed);
      const auto pts_ns = to_ns(timestamp);
      // Skip forward to this lane's aligned start: recorded clips do not share
      // a t=0, so the caller supplies how far into this file the common time
      // axis begins. Pacing starts at the aligned sample.
      if (!pacer_.past_start_offset(pts_ns)) {
        continue;
      }
      // Wrap on the period every lane shares rather than at this file's own end:
      // the clips differ in usable length by tens of milliseconds, so per-file
      // wrapping would let the lanes drift apart by that much on every pass.
      if (loop_sources_ && pacer_.pass_period_elapsed(pts_ns) &&
          seek_to_start(reader.Get())) {
        pacer_.advance_pass();
        counters_.reconnects.fetch_add(1, std::memory_order_relaxed);
        continue;
      }
      pacer_.pace(pts_ns, running);
      slot_pts_ = timestamp;
      publish_sample(sample.Get(), sequence++);
    }
  }

  void run() noexcept {
    running_.store(true, std::memory_order_release);
    // Media Foundation reader and D3D11 both prefer MTA on the worker thread.
    const auto com = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
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
          const auto delay = health.next_reconnect_delay();
          const auto until = std::chrono::steady_clock::now() + delay;
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
      set_error("unknown MF lane failure", true);
    }
    pool_.reset();
    if (SUCCEEDED(com)) {
      CoUninitialize();
    }
    running_.store(false, std::memory_order_release);
  }

  std::shared_ptr<D3D11Context> context_;
  swim::core::SourceConfig source_;
  std::uint32_t camera_index_{};
  swim::core::LatestFrameMailbox& mailbox_;
  swim::core::RuntimeCounters& counters_;
  swim::core::RunMode mode_;
  std::uint32_t surface_capacity_{};
  swim::core::RunLifecycle* lifecycle_{};
  bool loop_sources_{false};
  bool stop_at_eof_{false};
  swim::core::LanePacer pacer_;
  std::shared_ptr<MediaFoundationRuntime> mf_runtime_;
  std::shared_ptr<DecodedSurfacePool> pool_;
  // Set from the stream's media type before the first frame is published.
  std::uint32_t frame_width_{};
  std::uint32_t frame_height_{};
  swim::core::ColorMatrix color_matrix_{swim::core::ColorMatrix::bt709};
  bool full_range_{false};
  LONGLONG slot_pts_{0};
  mutable std::mutex thread_mutex_;
  std::thread worker_;
  std::atomic_bool stop_requested_{false};
  std::atomic_bool running_{false};
  std::atomic_bool failed_{false};
  mutable std::mutex error_mutex_;
  std::string last_error_;
};

MfSource::MfSource(std::shared_ptr<D3D11Context> context,
                   swim::core::SourceConfig source, std::uint32_t camera_index,
                   swim::core::LatestFrameMailbox& mailbox,
                   swim::core::RuntimeCounters& counters,
                   swim::core::RunMode mode, std::uint32_t ticket_capacity,
                   std::uint32_t surface_capacity,
                   swim::core::RunLifecycle* lifecycle, bool loop_sources,
                   bool stop_at_eof, std::chrono::milliseconds loop_period,
                   swim::core::SharedLaneOrigin* shared_origin)
    : impl_(std::make_unique<Impl>(std::move(context), std::move(source),
                                   camera_index, mailbox, counters, mode,
                                   ticket_capacity, surface_capacity,
                                   lifecycle, loop_sources, stop_at_eof,
                                   loop_period, shared_origin)) {}

MfSource::~MfSource() = default;

void MfSource::start() { impl_->start(); }
void MfSource::stop() noexcept { impl_->stop(); }
void MfSource::wait() { impl_->wait(); }
bool MfSource::running() const noexcept { return impl_->running(); }
bool MfSource::failed() const noexcept { return impl_->failed(); }
std::string MfSource::last_error() const { return impl_->last_error(); }

}  // namespace swim::d3d11


