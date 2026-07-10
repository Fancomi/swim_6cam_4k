#pragma once

#include <swim/core/frame.hpp>
#include <swim/core/latest_frame_mailbox.hpp>
#include <swim/core/metrics.hpp>
#include <swim/metal/metal_frame.hpp>

#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>

#include <atomic>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>

namespace swim::metal {

inline constexpr std::uint32_t kMetalDecodedSurfaceTag = 0x4D445331U;  // MDS1

class MetalDecodedSurfacePool;

// A slot is stable for the lifetime of its pool. Consumers must validate
// kMetalDecodedSurfaceTag and explicitly use view; this is not an old-style
// MetalFrameView native lease.
struct MetalDecodedSurface final {
  MetalFrameView view;
  std::uint32_t camera_index{};
  CVPixelBufferRef pixel_buffer = nullptr;
  CVMetalTextureRef luma_texture_ref = nullptr;
  CVMetalTextureRef chroma_texture_ref = nullptr;
  id<MTLTexture> luma = nil;
  id<MTLTexture> chroma = nil;
  std::atomic_uint32_t references{0};

  // Pool bookkeeping; callers must not modify these fields.
  std::uint32_t pool_index{};
  MetalDecodedSurfacePool* owner = nullptr;
  std::shared_ptr<MetalDecodedSurfacePool> lifetime_anchor;
};

struct VideoToolboxDecoderStats final {
  std::uint64_t submitted{};
  std::uint64_t callbacks{};
  std::uint64_t dropped{};
  std::uint64_t errors{};
  std::uint64_t late{};
};

enum class DecodeSubmitResult : std::uint8_t {
  submitted,
  dropped_pool,
  recoverable_error,
  stale_or_invalid,
};

enum class DecoderConfigurationFailure : std::uint8_t {
  invalid_format,
  hardware_unavailable,
  operational,
};

class DecoderConfigurationError final : public std::runtime_error {
 public:
  DecoderConfigurationError(DecoderConfigurationFailure failure,
                            std::string message)
      : std::runtime_error(std::move(message)), failure_(failure) {}

  DecoderConfigurationFailure failure() const noexcept { return failure_; }

 private:
  DecoderConfigurationFailure failure_;
};

class VideoToolboxDecoder final {
 public:
  VideoToolboxDecoder(
      std::shared_ptr<MetalContext> context, std::uint32_t camera_index,
      std::uint32_t ticket_capacity, std::uint32_t surface_capacity,
      swim::core::LatestFrameMailbox& mailbox,
      swim::core::RuntimeCounters& metrics);
  ~VideoToolboxDecoder();

  VideoToolboxDecoder(const VideoToolboxDecoder&) = delete;
  VideoToolboxDecoder& operator=(const VideoToolboxDecoder&) = delete;
  VideoToolboxDecoder(VideoToolboxDecoder&&) = delete;
  VideoToolboxDecoder& operator=(VideoToolboxDecoder&&) = delete;

  // Reconfiguration invalidates the previous generation, drains its callbacks,
  // and creates a new hardware-required H.264 decompression session.
  void configure(CMVideoFormatDescriptionRef format_description);

  // The sample and format description remain owned by the caller. VideoToolbox
  // retains everything needed after this function returns.
  DecodeSubmitResult decode(CMSampleBufferRef sample,
                            std::uint64_t decoder_generation) noexcept;

  void wait_for_asynchronous_frames();
  void drain();
  void invalidate() noexcept;

  bool using_hardware_acceleration() const noexcept;
  bool has_recoverable_error() const noexcept;
  OSStatus recoverable_error_status() const noexcept;
  std::uint64_t generation() const noexcept;
  VideoToolboxDecoderStats stats() const noexcept;

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace swim::metal
