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

// Decoded frame geometry is whatever the elementary stream declares: the pool
// runs 3840x2160 mp4, the underwater rig 1280x720 MPEG-TS. Only the constraints
// the Metal NV12 wrapping actually imposes are enforced (nonzero, even chroma).
constexpr std::uint32_t kMaxFrameDimension = 8192;

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
                          std::shared_ptr<swim::core::RuntimeCounterPublication>
                              publication)
      : context_(std::move(context)),
        capacity_(capacity),
        camera_index_(camera_index),
        publication_(std::move(publication)),
        slots_(std::make_unique<MetalDecodedSurface[]>(capacity)),
        free_mask_(surface_initial_mask(capacity)) {
    publication_->publish([this](auto& metrics) noexcept {
      metrics.decode_surface_capacity[camera_index_].store(
          capacity_, std::memory_order_relaxed);
    });
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
            in_use_.fetch_add(1, std::memory_order_relaxed) + 1;
        swim::core::record_atomic_max(high_water_, in_use);
        publication_->publish([this, in_use](auto& metrics) noexcept {
          metrics.decode_surface_in_use[camera_index_].store(
              in_use, std::memory_order_relaxed);
          metrics.decode_surface_high_water[camera_index_].store(
              high_water_.load(std::memory_order_relaxed),
              std::memory_order_relaxed);
        });
        return &slot;
      }
    }
    misses_.fetch_add(1, std::memory_order_relaxed);
    publication_->publish([this](auto& metrics) noexcept {
      metrics.decode_surface_pool_misses[camera_index_].fetch_add(
          1, std::memory_order_relaxed);
    });
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
    const auto remaining = in_use_.fetch_sub(1, std::memory_order_relaxed) - 1;
    publication_->publish([this, remaining](auto& metrics) noexcept {
      metrics.decode_surface_in_use[camera_index_].store(
          remaining, std::memory_order_relaxed);
    });
  }

  std::uint64_t in_use() const noexcept {
    return in_use_.load(std::memory_order_relaxed);
  }
  std::uint64_t high_water() const noexcept {
    return high_water_.load(std::memory_order_relaxed);
  }
  std::uint64_t misses() const noexcept {
    return misses_.load(std::memory_order_relaxed);
  }

 private:
  // Leases can outlive the decoder, so the pool also anchors the device/cache.
  std::shared_ptr<MetalContext> context_;
  const std::uint32_t capacity_;
  const std::uint32_t camera_index_;
  std::shared_ptr<swim::core::RuntimeCounterPublication> publication_;
  std::unique_ptr<MetalDecodedSurface[]> slots_;
  std::atomic_uint64_t free_mask_;
  std::atomic_uint64_t in_use_{};
  std::atomic_uint64_t high_water_{};
  std::atomic_uint64_t misses_{};
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
        publication_(
            std::make_shared<swim::core::RuntimeCounterPublication>(metrics)),
        tickets_(ticket_capacity),
        surfaces_(std::make_shared<MetalDecodedSurfacePool>(context_,
                                                            surface_capacity,
                                                            camera_index_,
                                                            publication_)),
        ticket_capacity_(ticket_capacity),
        surface_capacity_(surface_capacity) {
    if (context_ == nullptr || context_->device == nil ||
        context_->texture_cache == nullptr) {
      throw std::invalid_argument("VideoToolbox decoder requires a valid Metal context");
    }
    publication_->publish([this, ticket_capacity](auto& external) noexcept {
      external.native_decode_tickets.fetch_add(ticket_capacity,
                                               std::memory_order_relaxed);
      external.decode_ticket_capacity[camera_index_].store(
          ticket_capacity, std::memory_order_relaxed);
    });
  }

  ~Impl() {
    invalidate();
    publication_->finalize([this](auto& external) noexcept {
      external.decode_ticket_capacity[camera_index_].store(
          ticket_capacity_, std::memory_order_relaxed);
      external.decode_ticket_in_use[camera_index_].store(
          ticket_in_use_.load(std::memory_order_relaxed),
          std::memory_order_relaxed);
      external.decode_ticket_high_water[camera_index_].store(
          ticket_high_water_.load(std::memory_order_relaxed),
          std::memory_order_relaxed);
      external.decode_ticket_pool_misses[camera_index_].store(
          ticket_misses_.load(std::memory_order_relaxed),
          std::memory_order_relaxed);
      external.decode_surface_capacity[camera_index_].store(
          surface_capacity_, std::memory_order_relaxed);
      external.decode_surface_in_use[camera_index_].store(
          surfaces_->in_use(), std::memory_order_relaxed);
      external.decode_surface_high_water[camera_index_].store(
          surfaces_->high_water(), std::memory_order_relaxed);
      external.decode_surface_pool_misses[camera_index_].store(
          surfaces_->misses(), std::memory_order_relaxed);
    });
  }

  void configure(CMVideoFormatDescriptionRef format_description) {
    if (format_description == nullptr ||
        CMFormatDescriptionGetMediaType(format_description) != kCMMediaType_Video ||
        CMFormatDescriptionGetMediaSubType(format_description) != kCMVideoCodecType_H264) {
      throw DecoderConfigurationError{
          DecoderConfigurationFailure::invalid_format,
          "VideoToolbox decoder requires an H.264 video format"};
    }

    // Frame geometry is a property of the stream: 3840x2160 for the pool rig,
    // 1280x720 for the underwater rig. NV12 chroma is half-resolution, so only
    // even dimensions can be wrapped as Metal textures.
    const auto dimensions =
        CMVideoFormatDescriptionGetDimensions(format_description);
    if (dimensions.width <= 0 || dimensions.height <= 0 ||
        (dimensions.width & 1) != 0 || (dimensions.height & 1) != 0 ||
        dimensions.width > static_cast<std::int32_t>(kMaxFrameDimension) ||
        dimensions.height > static_cast<std::int32_t>(kMaxFrameDimension)) {
      throw DecoderConfigurationError{
          DecoderConfigurationFailure::invalid_format,
          "VideoToolbox decoder requires even frame dimensions within " +
              std::to_string(kMaxFrameDimension)};
    }

    std::lock_guard lock(session_mutex_);
    advance_generation_locked();
    destroy_session_locked();
    frame_width_ = static_cast<std::uint32_t>(dimensions.width);
    frame_height_ = static_cast<std::uint32_t>(dimensions.height);

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
    publication_->publish([](auto& external) noexcept {
      external.native_callback_wrappers.fetch_add(1,
                                                  std::memory_order_relaxed);
    });

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
                            std::uint64_t decoder_generation,
                            bool emit_frame) noexcept {
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
            ticket_in_use_.fetch_add(1, std::memory_order_relaxed) + 1;
        swim::core::record_atomic_max(ticket_high_water_, in_use);
        publication_->publish([this, in_use](auto& external) noexcept {
          external.decode_ticket_in_use[camera_index_].store(
              in_use, std::memory_order_relaxed);
          external.decode_ticket_high_water[camera_index_].store(
              ticket_high_water_.load(std::memory_order_relaxed),
              std::memory_order_relaxed);
        });
        ticket->camera_index = camera_index_;
        ticket->decoder_generation = decoder_generation;
        ticket->arrived_at = std::chrono::steady_clock::now();
        ticket->mailbox = &mailbox_;
        ticket->decoder = this;
      }
    }
    if (ticket == nullptr) {
      ticket_misses_.fetch_add(1, std::memory_order_relaxed);
      publication_->publish([this](auto& external) noexcept {
        external.pool_exhaustion.fetch_add(1, std::memory_order_relaxed);
        external.decode_ticket_pool_misses[camera_index_].fetch_add(
            1, std::memory_order_relaxed);
      });
      stats_.dropped.fetch_add(1, std::memory_order_relaxed);
      return DecodeSubmitResult::dropped_pool;
    }
    OSStatus status = kVTInvalidSessionErr;
    {
      std::lock_guard lock(session_mutex_);
      if (session_ != nullptr &&
          decoder_generation == generation_.load(std::memory_order_acquire)) {
        VTDecodeInfoFlags info_flags{};
        VTDecodeFrameFlags decode_flags =
            kVTDecodeFrame_EnableAsynchronousDecompression |
            kVTDecodeFrame_EnableTemporalProcessing;
        if (!emit_frame) {
          decode_flags |= kVTDecodeFrame_DoNotOutputFrame;
        }
        status = VTDecompressionSessionDecodeFrame(
            session_, sample, decode_flags, ticket, &info_flags);
      }
    }
    {
      swim::core::HotPathAllocationScope hot_path;
      if (status != noErr) {
        tickets_.release(ticket);
        const auto remaining =
            ticket_in_use_.fetch_sub(1, std::memory_order_relaxed) - 1;
        publication_->publish([this, remaining](auto& external) noexcept {
          external.decode_ticket_in_use[camera_index_].store(
              remaining, std::memory_order_relaxed);
        });
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
    @autoreleasepool {
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
  }

  void release_ticket(DecodeTicket* ticket) noexcept {
    tickets_.release(ticket);
    const auto remaining =
        ticket_in_use_.fetch_sub(1, std::memory_order_relaxed) - 1;
    publication_->publish([this, remaining](auto& external) noexcept {
      external.decode_ticket_in_use[camera_index_].store(
          remaining, std::memory_order_relaxed);
    });
  }

  void record_malformed() noexcept {
    publication_->publish([](auto& external) noexcept {
      external.malformed.fetch_add(1, std::memory_order_relaxed);
    });
  }

  void record_pool_exhaustion() noexcept {
    publication_->publish([](auto& external) noexcept {
      external.pool_exhaustion.fetch_add(1, std::memory_order_relaxed);
    });
  }

  void record_texture_wrapper() noexcept {
    publication_->publish([](auto& external) noexcept {
      external.native_texture_wrappers.fetch_add(1,
                                                 std::memory_order_relaxed);
    });
  }

  void handle_callback(DecodeTicket* ticket, OSStatus status,
                       VTDecodeInfoFlags info_flags,
                       CVImageBufferRef image_buffer,
                       CMTime presentation_time) noexcept {
    const auto release_current_ticket = [this, ticket] {
      release_ticket(ticket);
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
        release_current_ticket();
        return;
      }
      if (status != noErr) {
        stats_.errors.fetch_add(1, std::memory_order_relaxed);
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        set_recoverable_error_locked(status);
        release_current_ticket();
        return;
      }
      if (image_buffer == nullptr ||
          (info_flags & kVTDecodeInfo_FrameDropped) != 0) {
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        release_current_ticket();
        return;
      }
      pixel_buffer = static_cast<CVPixelBufferRef>(image_buffer);
      if (CVPixelBufferGetWidth(pixel_buffer) != frame_width_ ||
          CVPixelBufferGetHeight(pixel_buffer) != frame_height_ ||
          CVPixelBufferGetPixelFormatType(pixel_buffer) !=
              kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange ||
          CVPixelBufferGetPlaneCount(pixel_buffer) != 2) {
        record_malformed();
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        stats_.errors.fetch_add(1, std::memory_order_relaxed);
        set_recoverable_error_locked(kVTVideoDecoderMalfunctionErr);
        release_current_ticket();
        return;
      }
      matrix = color_matrix(pixel_buffer);
      if (!matrix.has_value()) {
        record_malformed();
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        stats_.errors.fetch_add(1, std::memory_order_relaxed);
        set_recoverable_error_locked(kVTVideoDecoderMalfunctionErr);
        release_current_ticket();
        return;
      }
      if (!CMTIME_IS_VALID(presentation_time) ||
          !CMTIME_IS_NUMERIC(presentation_time) ||
          presentation_time.timescale <= 0) {
        record_malformed();
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        stats_.errors.fetch_add(1, std::memory_order_relaxed);
        set_recoverable_error_locked(kVTVideoDecoderMalfunctionErr);
        release_current_ticket();
        return;
      }
      if (CMTIME_IS_VALID(last_published_pts_) &&
          CMTimeCompare(presentation_time, last_published_pts_) <= 0) {
        stats_.late.fetch_add(1, std::memory_order_relaxed);
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        release_current_ticket();
        return;
      }
      surface = surfaces_->try_acquire();
      if (surface == nullptr) {
        record_pool_exhaustion();
        stats_.dropped.fetch_add(1, std::memory_order_relaxed);
        release_current_ticket();
        return;
      }
    }

    surface->camera_index = ticket->camera_index;
    CFRetain(pixel_buffer);
    surface->pixel_buffer = pixel_buffer;
    auto texture_status = CVMetalTextureCacheCreateTextureFromImage(
        kCFAllocatorDefault, context_->texture_cache, surface->pixel_buffer,
        nullptr, MTLPixelFormatR8Unorm, frame_width_, frame_height_, 0,
        &surface->luma_texture_ref);
    if (texture_status == kCVReturnSuccess &&
        surface->luma_texture_ref != nullptr) {
      record_texture_wrapper();
      surface->luma = CVMetalTextureGetTexture(surface->luma_texture_ref);
    }
    if (texture_status == kCVReturnSuccess && surface->luma != nil) {
      texture_status = CVMetalTextureCacheCreateTextureFromImage(
          kCFAllocatorDefault, context_->texture_cache, surface->pixel_buffer,
          nullptr, MTLPixelFormatRG8Unorm, frame_width_ / 2,
          frame_height_ / 2, 1, &surface->chroma_texture_ref);
      if (texture_status == kCVReturnSuccess &&
          surface->chroma_texture_ref != nullptr) {
        record_texture_wrapper();
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
      release_current_ticket();
      return;
    }

    {
      swim::core::HotPathAllocationScope hot_path;
      swim::core::FrameMetadata metadata;
      metadata.camera_index = ticket->camera_index;
      metadata.width = frame_width_;
      metadata.height = frame_height_;
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
    publication_->publish([this](auto& external) noexcept {
      external.decoded.fetch_add(1, std::memory_order_relaxed);
      external.published.fetch_add(1, std::memory_order_relaxed);
      external.camera_decoded[camera_index_].fetch_add(
          1, std::memory_order_relaxed);
      external.camera_published[camera_index_].fetch_add(
          1, std::memory_order_relaxed);
    });
    release_current_ticket();
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
  std::shared_ptr<swim::core::RuntimeCounterPublication> publication_;
  DecodeTicketPool tickets_;
  std::shared_ptr<MetalDecodedSurfacePool> surfaces_;
  const std::uint32_t ticket_capacity_;
  const std::uint32_t surface_capacity_;
  // Set by configure() from the stream's format description; read by the
  // decompression callback, which only runs for the generation configure()
  // installed, so the session mutex covers every transition.
  std::uint32_t frame_width_{};
  std::uint32_t frame_height_{};
  std::atomic_uint64_t ticket_in_use_{};
  std::atomic_uint64_t ticket_high_water_{};
  std::atomic_uint64_t ticket_misses_{};
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
    CMSampleBufferRef sample, std::uint64_t decoder_generation,
    bool emit_frame) noexcept {
  return impl_->decode(sample, decoder_generation, emit_frame);
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
