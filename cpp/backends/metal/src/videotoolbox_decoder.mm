#include <swim/metal/videotoolbox_decoder.hpp>

#include <swim/core/hot_path_allocations.hpp>
#include <swim/core/render_completion_gate.hpp>

#import <Foundation/Foundation.h>
#import <VideoToolbox/VideoToolbox.h>

#include <algorithm>
#include <atomic>
#include <bit>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <limits>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>

namespace swim::metal {
namespace {

constexpr std::uint32_t kRequiredWidth = 3840;
constexpr std::uint32_t kRequiredHeight = 2160;

std::uint64_t initial_mask(std::uint32_t capacity) {
  if (capacity == 0 || capacity > 64) {
    throw std::invalid_argument("decoder pool capacity must be between 1 and 64");
  }
  return capacity == 64 ? ~std::uint64_t{0}
                        : (std::uint64_t{1} << capacity) - 1;
}

std::uint64_t surface_initial_mask(std::uint32_t capacity) {
  if (capacity < 4 || capacity > 64) {
    throw std::invalid_argument(
        "decoded surface pool capacity must be between 4 and 64");
  }
  return initial_mask(capacity);
}

std::runtime_error vt_error(const char* operation, OSStatus status) {
  return std::runtime_error(std::string(operation) + " failed (OSStatus " +
                            std::to_string(status) + ")");
}

std::optional<swim::core::ColorMatrix> color_matrix(
    CVPixelBufferRef pixel_buffer) noexcept {
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-declarations"
  auto* value = CVBufferGetAttachment(pixel_buffer,
                                      kCVImageBufferYCbCrMatrixKey, nullptr);
#pragma clang diagnostic pop
  if (value != nullptr &&
      CFEqual(value, kCVImageBufferYCbCrMatrix_ITU_R_601_4)) {
    return swim::core::ColorMatrix::bt601;
  }
  if (value != nullptr &&
      CFEqual(value, kCVImageBufferYCbCrMatrix_ITU_R_2020)) {
    return swim::core::ColorMatrix::bt2020;
  }
  if (value != nullptr &&
      CFEqual(value, kCVImageBufferYCbCrMatrix_ITU_R_709_2)) {
    return swim::core::ColorMatrix::bt709;
  }
  return std::nullopt;
}

void retain_surface(void* native) noexcept {
  auto* surface = static_cast<MetalDecodedSurface*>(native);
  if (surface == nullptr ||
      surface->references.fetch_add(1, std::memory_order_relaxed) == 0) {
    std::terminate();
  }
}

void release_surface(void* native) noexcept;

}  // namespace

class MetalDecodedSurfacePool final
    : public std::enable_shared_from_this<MetalDecodedSurfacePool> {
 public:
  MetalDecodedSurfacePool(std::shared_ptr<MetalContext> context,
                          std::uint32_t capacity,
                          std::uint32_t camera_index,
                          swim::core::RuntimeCounters& metrics)
      : context_(std::move(context)),
        capacity_(capacity),
        camera_index_(camera_index),
        metrics_(metrics),
        slots_(std::make_unique<MetalDecodedSurface[]>(capacity)),
        free_mask_(surface_initial_mask(capacity)) {
    metrics_.decode_surface_capacity[camera_index_].store(
        capacity_, std::memory_order_relaxed);
    for (std::uint32_t index = 0; index < capacity_; ++index) {
      slots_[index].pool_index = index;
      slots_[index].owner = this;
    }
  }

  ~MetalDecodedSurfacePool() noexcept {
    if (free_mask_.load(std::memory_order_acquire) !=
        surface_initial_mask(capacity_)) {
      std::terminate();
    }
  }

  MetalDecodedSurface* try_acquire() noexcept {
    auto available = free_mask_.load(std::memory_order_relaxed);
    while (available != 0) {
      const auto index = static_cast<std::uint32_t>(std::countr_zero(available));
      const auto bit = std::uint64_t{1} << index;
      if (free_mask_.compare_exchange_weak(
              available, available & ~bit, std::memory_order_acquire,
              std::memory_order_relaxed)) {
        auto& slot = slots_[index];
        slot.lifetime_anchor = shared_from_this();
        slot.references.store(1, std::memory_order_release);
        const auto in_use =
            metrics_.decode_surface_in_use[camera_index_].fetch_add(
                1, std::memory_order_relaxed) + 1;
        swim::core::record_atomic_max(
            metrics_.decode_surface_high_water[camera_index_], in_use);
        return &slot;
      }
    }
    metrics_.decode_surface_pool_misses[camera_index_].fetch_add(
        1, std::memory_order_relaxed);
    return nullptr;
  }

  void release(MetalDecodedSurface* surface) noexcept {
    const auto index = surface->pool_index;
    surface->view = {};
    surface->camera_index = 0;
    surface->luma = nil;
    surface->chroma = nil;
    if (surface->luma_texture_ref != nullptr) {
      CFRelease(surface->luma_texture_ref);
      surface->luma_texture_ref = nullptr;
    }
    if (surface->chroma_texture_ref != nullptr) {
      CFRelease(surface->chroma_texture_ref);
      surface->chroma_texture_ref = nullptr;
    }
    if (surface->pixel_buffer != nullptr) {
      CFRelease(surface->pixel_buffer);
      surface->pixel_buffer = nullptr;
    }
    free_mask_.fetch_or(std::uint64_t{1} << index, std::memory_order_release);
    metrics_.decode_surface_in_use[camera_index_].fetch_sub(
        1, std::memory_order_relaxed);
  }

 private:
  // Leases can outlive the decoder, so the pool also anchors the device/cache.
  std::shared_ptr<MetalContext> context_;
  const std::uint32_t capacity_;
  const std::uint32_t camera_index_;
  swim::core::RuntimeCounters& metrics_;
  std::unique_ptr<MetalDecodedSurface[]> slots_;
  std::atomic_uint64_t free_mask_;
};

namespace {

void release_surface(void* native) noexcept {
  auto* surface = static_cast<MetalDecodedSurface*>(native);
  if (surface == nullptr) {
    return;
  }
  const auto previous =
      surface->references.fetch_sub(1, std::memory_order_release);
  if (previous == 0) {
    std::terminate();
  }
  if (previous == 1) {
    std::atomic_thread_fence(std::memory_order_acquire);
    auto pool = std::move(surface->lifetime_anchor);
    if (pool == nullptr || surface->owner != pool.get()) {
      std::terminate();
    }
    pool->release(surface);
  }
}

struct DecodeTicket final {
  std::uint32_t pool_index{};
  std::uint32_t camera_index{};
  std::uint64_t decoder_generation{};
  std::chrono::steady_clock::time_point arrived_at{};
  swim::core::LatestFrameMailbox* mailbox{};
  void* decoder{};
};

class DecodeTicketPool final {
 public:
  explicit DecodeTicketPool(std::uint32_t capacity)
      : capacity_(capacity),
        tickets_(std::make_unique<DecodeTicket[]>(capacity)),
        free_mask_(initial_mask(capacity)) {
    for (std::uint32_t index = 0; index < capacity_; ++index) {
      tickets_[index].pool_index = index;
    }
  }

  ~DecodeTicketPool() noexcept {
    if (free_mask_.load(std::memory_order_acquire) != initial_mask(capacity_)) {
      std::terminate();
    }
  }

  DecodeTicket* try_acquire() noexcept {
    auto available = free_mask_.load(std::memory_order_relaxed);
    while (available != 0) {
      const auto index = static_cast<std::uint32_t>(std::countr_zero(available));
      const auto bit = std::uint64_t{1} << index;
      if (free_mask_.compare_exchange_weak(
              available, available & ~bit, std::memory_order_acquire,
              std::memory_order_relaxed)) {
        return &tickets_[index];
      }
    }
    return nullptr;
  }

  void release(DecodeTicket* ticket) noexcept {
    const auto index = ticket->pool_index;
    *ticket = {};
    ticket->pool_index = index;
    const auto bit = std::uint64_t{1} << index;
    if ((free_mask_.fetch_or(bit, std::memory_order_release) & bit) != 0) {
      std::terminate();
    }
  }

 private:
  const std::uint32_t capacity_;
  std::unique_ptr<DecodeTicket[]> tickets_;
  std::atomic_uint64_t free_mask_;
};

struct AtomicStats final {
  std::atomic_uint64_t submitted{};
  std::atomic_uint64_t callbacks{};
  std::atomic_uint64_t dropped{};
  std::atomic_uint64_t errors{};
  std::atomic_uint64_t late{};
};

}  // namespace

class VideoToolboxDecoder::Impl final {
 public:
  Impl(std::shared_ptr<MetalContext> context, std::uint32_t camera_index,
       std::uint32_t ticket_capacity, std::uint32_t surface_capacity,
       swim::core::LatestFrameMailbox& mailbox,
       swim::core::RuntimeCounters& metrics)
      : context_(std::move(context)),
        camera_index_(camera_index),
        mailbox_(mailbox),
        metrics_(metrics),
        tickets_(ticket_capacity),
        surfaces_(std::make_shared<MetalDecodedSurfacePool>(context_,
                                                            surface_capacity,
                                                            camera_index_,
                                                            metrics_)) {
    if (context_ == nullptr || context_->device == nil ||
        context_->texture_cache == nullptr) {
      throw std::invalid_argument("VideoToolbox decoder requires a valid Metal context");
    }
    metrics_.native_decode_tickets.fetch_add(ticket_capacity,
                                             std::memory_order_relaxed);
    metrics_.decode_ticket_capacity[camera_index_].store(
        ticket_capacity, std::memory_order_relaxed);
  }

  ~Impl() { invalidate(); }

  void configure(CMVideoFormatDescriptionRef format_description) {
    if (format_description == nullptr ||
        CMFormatDescriptionGetMediaType(format_description) != kCMMediaType_Video ||
        CMFormatDescriptionGetMediaSubType(format_description) != kCMVideoCodecType_H264) {
      throw DecoderConfigurationError{
          DecoderConfigurationFailure::invalid_format,
          "VideoToolbox decoder requires an H.264 video format"};
    }

    std::lock_guard lock(session_mutex_);
    advance_generation_locked();
    destroy_session_locked();

    NSDictionary* decoder_specification = @{
      (__bridge NSString*)kVTVideoDecoderSpecification_EnableHardwareAcceleratedVideoDecoder : @YES,
      (__bridge NSString*)kVTVideoDecoderSpecification_RequireHardwareAcceleratedVideoDecoder : @YES,
    };
    NSDictionary* destination_attributes = @{
      (__bridge NSString*)kCVPixelBufferPixelFormatTypeKey :
          @(kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange),
      (__bridge NSString*)kCVPixelBufferMetalCompatibilityKey : @YES,
      (__bridge NSString*)kCVPixelBufferIOSurfacePropertiesKey : @{},
    };
    VTDecompressionOutputCallbackRecord callback{
        &Impl::decompression_callback, this};
    auto status = VTDecompressionSessionCreate(
        kCFAllocatorDefault, format_description,
        (__bridge CFDictionaryRef)decoder_specification,
        (__bridge CFDictionaryRef)destination_attributes, &callback, &session_);
    if (status != noErr || session_ == nullptr) {
      session_ = nullptr;
      const auto failure =
          status == kVTCouldNotFindVideoDecoderErr
              ? DecoderConfigurationFailure::hardware_unavailable
              : status == kVTVideoDecoderUnsupportedDataFormatErr ||
                        status == paramErr
                    ? DecoderConfigurationFailure::invalid_format
                    : DecoderConfigurationFailure::operational;
      throw DecoderConfigurationError{
          failure,
          "VTDecompressionSessionCreate failed (OSStatus " +
              std::to_string(status) + ")"};
    }
    metrics_.native_callback_wrappers.fetch_add(1, std::memory_order_relaxed);

    CFTypeRef hardware_value = nullptr;
    status = VTSessionCopyProperty(
        session_, kVTDecompressionPropertyKey_UsingHardwareAcceleratedVideoDecoder,
        kCFAllocatorDefault, &hardware_value);
    const bool hardware = status == noErr && hardware_value != nullptr &&
                          CFGetTypeID(hardware_value) == CFBooleanGetTypeID() &&
                          CFBooleanGetValue(static_cast<CFBooleanRef>(hardware_value));
    if (hardware_value != nullptr) {
      CFRelease(hardware_value);
    }
    hardware_.store(hardware, std::memory_order_release);
    if (!hardware) {
      destroy_session_locked();
      throw DecoderConfigurationError{
          DecoderConfigurationFailure::hardware_unavailable,
          "hardware-accelerated VideoToolbox decode is required"};
    }
  }

  DecodeSubmitResult decode(CMSampleBufferRef sample,
                            std::uint64_t decoder_generation) noexcept {
    if (sample == nullptr || !CMSampleBufferDataIsReady(sample) ||
        CMSampleBufferGetNumSamples(sample) != 1 ||
        decoder_generation != generation_.load(std::memory_order_acquire)) {
      return DecodeSubmitResult::stale_or_invalid;
    }
    DecodeTicket* ticket = nullptr;
    {
      swim::core::HotPathAllocationScope hot_path;
      ticket = tickets_.try_acquire();
      if (ticket != nullptr) {
        const auto in_use =
            metrics_.decode_ticket_in_use[camera_index_].fetch_add(
                1, std::memory_order_relaxed) + 1;
        swim::core::record_atomic_max(
            metrics_.decode_ticket_high_water[camera_index_], in_use);
        ticket->camera_index = camera_index_;
        ticket->decoder_generation = decoder_generation;
        ticket->arrived_at = std::chrono::steady_clock::now();
        ticket->mailbox = &mailbox_;
        ticket->decoder = this;
      }
    }
    if (ticket == nullptr) {
      metrics_.pool_exhaustion.fetch_add(1, std::memory_order_relaxed);
      metrics_.decode_ticket_pool_misses[camera_index_].fetch_add(
          1, std::memory_order_relaxed);
      stats_.dropped.fetch_add(1, std::memory_order_relaxed);
      return DecodeSubmitResult::dropped_pool;
    }
    OSStatus status = kVTInvalidSessionErr;
    {
      std::lock_guard lock(session_mutex_);
      if (session_ != nullptr &&
          decoder_generation == generation_.load(std::memory_order_acquire)) {
        VTDecodeInfoFlags info_flags{};
        status = VTDecompressionSessionDecodeFrame(
            session_, sample,
            kVTDecodeFrame_EnableAsynchronousDecompression |
                kVTDecodeFrame_EnableTemporalProcessing,
            ticket, &info_flags);
      }
    }
    {
      swim::core::HotPathAllocationScope hot_path;
      if (status != noErr) {
        tickets_.release(ticket);
        metrics_.decode_ticket_in_use[camera_index_].fetch_sub(
            1, std::memory_order_relaxed);
        stats_.errors.fetch_add(1, std::memory_order_relaxed);
        set_recoverable_error_for_generation(status, decoder_generation);
        return DecodeSubmitResult::recoverable_error;
      }
      stats_.submitted.fetch_add(1, std::memory_order_relaxed);
    }
    // For a successful submission VideoToolbox owns the ticket until it calls
    // the output callback, including synchronous FrameDropped callbacks.
    return DecodeSubmitResult::submitted;
  }

  void wait() {
    std::lock_guard lock(session_mutex_);
    if (session_ != nullptr) {
      const auto status = VTDecompressionSessionWaitForAsynchronousFrames(session_);
      if (status != noErr) {
        throw vt_error("VTDecompressionSessionWaitForAsynchronousFrames", status);
      }
    }
  }

  void drain() {
    std::lock_guard lock(session_mutex_);
    if (session_ == nullptr) {
      return;
    }
    auto status = VTDecompressionSessionFinishDelayedFrames(session_);
    if (status == noErr) {
      status = VTDecompressionSessionWaitForAsynchronousFrames(session_);
    }
    if (status != noErr) {
      throw vt_error("draining VTDecompressionSession", status);
    }
  }

  void invalidate() noexcept {
    std::lock_guard lock(session_mutex_);
    advance_generation_locked();
    destroy_session_locked();
  }

  bool hardware() const noexcept {
    return hardware_.load(std::memory_order_acquire);
  }

  bool has_recoverable_error() const noexcept {
    return recoverable_error_.load(std::memory_order_acquire);
  }

  OSStatus recoverable_error_status() const noexcept {
    return recoverable_error_status_.load(std::memory_order_relaxed);
  }

  std::uint64_t generation() const noexcept {
    return generation_.load(std::memory_order_acquire);
  }

  VideoToolboxDecoderStats stats() const noexcept {
    return {stats_.submitted.load(std::memory_order_relaxed),
            stats_.callbacks.load(std::memory_order_relaxed),
            stats_.dropped.load(std::memory_order_relaxed),
            stats_.errors.load(std::memory_order_relaxed),
            stats_.late.load(std::memory_order_relaxed)};
  }

 private:
  static void decompression_callback(void* refcon, void* source_refcon,
                                     OSStatus status,
                                     VTDecodeInfoFlags info_flags,
                                     CVImageBufferRef image_buffer,
                                     CMTime presentation_time,
                                     CMTime presentation_duration) noexcept {
    static_cast<void>(refcon);
    static_cast<void>(presentation_duration);
    auto* ticket = static_cast<DecodeTicket*>(source_refcon);
    if (ticket == nullptr || ticket->decoder == nullptr) {
      return;
    }
    auto* decoder = static_cast<Impl*>(ticket->decoder);
    decoder->handle_callback(ticket, status, info_flags, image_buffer,
                             presentation_time);
  }

  void handle_callback(DecodeTicket* ticket, OSStatus status,
                       VTDecodeInfoFlags info_flags,
                       CVImageBufferRef image_buffer,
                       CMTime presentation_time) noexcept {
    const auto release_ticket = [this, ticket] {
      tickets_.release(ticket);
      metrics_.decode_ticket_in_use[camera_index_].fetch_sub(
          1, std::memory_order_relaxed);
    };
    std::lock_guard publish_lock(publish_mutex_);
    CVPixelBufferRef pixel_buffer = nullptr;
    std::optional<swim::core::ColorMatrix> matrix;
    MetalDecodedSurface* surface = nullptr;
    {
      swim::core::HotPathAllocationScope hot_path;
      stats_.callbacks.fetch_add(1, std::memory_order_relaxed);
      if (ticket->decoder_generation !=
          generation_.load(std::memory_order_acquire)) {
        stats_.late.fetch_add(1, std::memory_order_relaxed);
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        release_ticket();
        return;
      }
      if (status != noErr) {
        stats_.errors.fetch_add(1, std::memory_order_relaxed);
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        set_recoverable_error_locked(status);
        release_ticket();
        return;
      }
      if (image_buffer == nullptr ||
          (info_flags & kVTDecodeInfo_FrameDropped) != 0) {
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        release_ticket();
        return;
      }
      pixel_buffer = static_cast<CVPixelBufferRef>(image_buffer);
      if (CVPixelBufferGetWidth(pixel_buffer) != kRequiredWidth ||
          CVPixelBufferGetHeight(pixel_buffer) != kRequiredHeight ||
          CVPixelBufferGetPixelFormatType(pixel_buffer) !=
              kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange ||
          CVPixelBufferGetPlaneCount(pixel_buffer) != 2) {
        metrics_.malformed.fetch_add(1, std::memory_order_relaxed);
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        stats_.errors.fetch_add(1, std::memory_order_relaxed);
        set_recoverable_error_locked(kVTVideoDecoderMalfunctionErr);
        release_ticket();
        return;
      }
      matrix = color_matrix(pixel_buffer);
      if (!matrix.has_value()) {
        metrics_.malformed.fetch_add(1, std::memory_order_relaxed);
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        stats_.errors.fetch_add(1, std::memory_order_relaxed);
        set_recoverable_error_locked(kVTVideoDecoderMalfunctionErr);
        release_ticket();
        return;
      }
      if (!CMTIME_IS_VALID(presentation_time) ||
          !CMTIME_IS_NUMERIC(presentation_time) ||
          presentation_time.timescale <= 0) {
        metrics_.malformed.fetch_add(1, std::memory_order_relaxed);
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        stats_.errors.fetch_add(1, std::memory_order_relaxed);
        set_recoverable_error_locked(kVTVideoDecoderMalfunctionErr);
        release_ticket();
        return;
      }
      if (CMTIME_IS_VALID(last_published_pts_) &&
          CMTimeCompare(presentation_time, last_published_pts_) <= 0) {
        stats_.late.fetch_add(1, std::memory_order_relaxed);
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        release_ticket();
        return;
      }
      surface = surfaces_->try_acquire();
      if (surface == nullptr) {
        metrics_.pool_exhaustion.fetch_add(1, std::memory_order_relaxed);
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        release_ticket();
        return;
      }
    }

    surface->camera_index = ticket->camera_index;
    CFRetain(pixel_buffer);
    surface->pixel_buffer = pixel_buffer;
    auto texture_status = CVMetalTextureCacheCreateTextureFromImage(
        kCFAllocatorDefault, context_->texture_cache, surface->pixel_buffer,
        nullptr, MTLPixelFormatR8Unorm, kRequiredWidth, kRequiredHeight, 0,
        &surface->luma_texture_ref);
    if (texture_status == kCVReturnSuccess &&
        surface->luma_texture_ref != nullptr) {
      metrics_.native_texture_wrappers.fetch_add(1, std::memory_order_relaxed);
      surface->luma = CVMetalTextureGetTexture(surface->luma_texture_ref);
    }
    if (texture_status == kCVReturnSuccess && surface->luma != nil) {
      texture_status = CVMetalTextureCacheCreateTextureFromImage(
          kCFAllocatorDefault, context_->texture_cache, surface->pixel_buffer,
          nullptr, MTLPixelFormatRG8Unorm, kRequiredWidth / 2,
          kRequiredHeight / 2, 1, &surface->chroma_texture_ref);
      if (texture_status == kCVReturnSuccess &&
          surface->chroma_texture_ref != nullptr) {
        metrics_.native_texture_wrappers.fetch_add(1, std::memory_order_relaxed);
        surface->chroma = CVMetalTextureGetTexture(surface->chroma_texture_ref);
      }
    }
    if (texture_status != kCVReturnSuccess || surface->luma == nil ||
        surface->chroma == nil) {
      stats_.errors.fetch_add(1, std::memory_order_relaxed);
      stats_.dropped.fetch_add(1, std::memory_order_relaxed);
      set_recoverable_error_locked(
          texture_status == kCVReturnSuccess
              ? kVTVideoDecoderMalfunctionErr
              : static_cast<OSStatus>(texture_status));
      release_surface(surface);
      release_ticket();
      return;
    }

    {
      swim::core::HotPathAllocationScope hot_path;
      swim::core::FrameMetadata metadata;
      metadata.camera_index = ticket->camera_index;
      metadata.width = kRequiredWidth;
      metadata.height = kRequiredHeight;
      metadata.sequence = ++published_sequence_;
      metadata.decoder_generation = ticket->decoder_generation;
      metadata.pts_value = presentation_time.value;
      metadata.pts_timescale = presentation_time.timescale;
      metadata.arrived_at = ticket->arrived_at;
      metadata.decoded_at = std::chrono::steady_clock::now();
      metadata.pixel_format = swim::core::PixelFormat::nv12_video_range;
      metadata.color_matrix = *matrix;
      metadata.discontinuity = first_frame_of_generation_;

      surface->view.rgba = nil;
      surface->view.luma = surface->luma;
      surface->view.chroma = surface->chroma;
      surface->view.metadata = metadata;

      swim::core::NativeLeaseOps ops{
          &retain_surface, &release_surface, kMetalDecodedSurfaceTag};
      ticket->mailbox->publish(
          swim::core::FrameLease(surface, ops, metadata));
    }
    last_published_pts_ = presentation_time;
    first_frame_of_generation_ = false;
    metrics_.decoded.fetch_add(1, std::memory_order_relaxed);
    metrics_.published.fetch_add(1, std::memory_order_relaxed);
    metrics_.camera_decoded[camera_index_].fetch_add(
        1, std::memory_order_relaxed);
    metrics_.camera_published[camera_index_].fetch_add(
        1, std::memory_order_relaxed);
    release_ticket();
  }

  void destroy_session_locked() noexcept {
    hardware_.store(false, std::memory_order_release);
    if (session_ == nullptr) {
      return;
    }
    static_cast<void>(VTDecompressionSessionFinishDelayedFrames(session_));
    static_cast<void>(VTDecompressionSessionWaitForAsynchronousFrames(session_));
    VTDecompressionSessionInvalidate(session_);
    CFRelease(session_);
    session_ = nullptr;
  }

  void set_recoverable_error_locked(OSStatus status) noexcept {
    recoverable_error_status_.store(status, std::memory_order_relaxed);
    recoverable_error_.store(true, std::memory_order_release);
  }

  void set_recoverable_error_for_generation(
      OSStatus status, std::uint64_t decoder_generation) noexcept {
    std::lock_guard publish_lock(publish_mutex_);
    if (decoder_generation == generation_.load(std::memory_order_acquire)) {
      set_recoverable_error_locked(status);
    }
  }

  // session_mutex_ is held by the caller. Taking the publication lock makes
  // generation advance and the callback's final generation check atomic with
  // respect to publication: an old callback either publishes before the
  // advance or observes the new generation and drops.
  void advance_generation_locked() noexcept {
    std::lock_guard publish_lock(publish_mutex_);
    generation_.fetch_add(1, std::memory_order_acq_rel);
    recoverable_error_status_.store(noErr, std::memory_order_relaxed);
    recoverable_error_.store(false, std::memory_order_release);
    last_published_pts_ = kCMTimeInvalid;
    first_frame_of_generation_ = true;
  }

  std::shared_ptr<MetalContext> context_;
  const std::uint32_t camera_index_;
  swim::core::LatestFrameMailbox& mailbox_;
  swim::core::RuntimeCounters& metrics_;
  DecodeTicketPool tickets_;
  std::shared_ptr<MetalDecodedSurfacePool> surfaces_;
  mutable std::mutex session_mutex_;
  std::mutex publish_mutex_;
  VTDecompressionSessionRef session_ = nullptr;
  std::atomic_uint64_t generation_{0};
  std::atomic_bool hardware_{false};
  std::atomic_bool recoverable_error_{false};
  std::atomic<OSStatus> recoverable_error_status_{noErr};
  CMTime last_published_pts_{kCMTimeInvalid};
  std::uint64_t published_sequence_{};
  bool first_frame_of_generation_{true};
  AtomicStats stats_;
};

VideoToolboxDecoder::VideoToolboxDecoder(
    std::shared_ptr<MetalContext> context, std::uint32_t camera_index,
    std::uint32_t ticket_capacity, std::uint32_t surface_capacity,
    swim::core::LatestFrameMailbox& mailbox,
    swim::core::RuntimeCounters& metrics)
    : impl_(std::make_unique<Impl>(std::move(context), camera_index,
                                   ticket_capacity, surface_capacity, mailbox,
                                   metrics)) {}

VideoToolboxDecoder::~VideoToolboxDecoder() = default;

void VideoToolboxDecoder::configure(
    CMVideoFormatDescriptionRef format_description) {
  impl_->configure(format_description);
}

DecodeSubmitResult VideoToolboxDecoder::decode(
    CMSampleBufferRef sample, std::uint64_t decoder_generation) noexcept {
  return impl_->decode(sample, decoder_generation);
}

void VideoToolboxDecoder::wait_for_asynchronous_frames() { impl_->wait(); }

void VideoToolboxDecoder::drain() { impl_->drain(); }

void VideoToolboxDecoder::invalidate() noexcept { impl_->invalidate(); }

bool VideoToolboxDecoder::using_hardware_acceleration() const noexcept {
  return impl_->hardware();
}

bool VideoToolboxDecoder::has_recoverable_error() const noexcept {
  return impl_->has_recoverable_error();
}

OSStatus VideoToolboxDecoder::recoverable_error_status() const noexcept {
  return impl_->recoverable_error_status();
}

std::uint64_t VideoToolboxDecoder::generation() const noexcept {
  return impl_->generation();
}

VideoToolboxDecoderStats VideoToolboxDecoder::stats() const noexcept {
  return impl_->stats();
}

}  // namespace swim::metal
