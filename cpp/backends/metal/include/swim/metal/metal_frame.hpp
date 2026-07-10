#pragma once

#include <swim/core/frame.hpp>

#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>

#include <atomic>
#include <cstdint>
#include <memory>
#include <optional>

namespace swim::metal {

class MetalOutputPool;

struct MetalContext final {
  id<MTLDevice> device = nil;
  id<MTLCommandQueue> command_queue = nil;
  CVMetalTextureCacheRef texture_cache = nullptr;

  MetalContext() = default;
  ~MetalContext();
  MetalContext(const MetalContext&) = delete;
  MetalContext& operator=(const MetalContext&) = delete;
};

struct MetalFrameView final {
  id<MTLTexture> rgba = nil;
  id<MTLTexture> luma = nil;
  id<MTLTexture> chroma = nil;
  swim::core::FrameMetadata metadata;
};

struct MetalOutputSlot final {
  CVPixelBufferRef pixel_buffer = nullptr;
  CVMetalTextureRef texture_ref = nullptr;
  id<MTLTexture> texture = nil;
  std::atomic_uint32_t references{0};
  std::uint32_t pool_index = 0;
  MetalOutputPool* owner = nullptr;
};

class MetalOutputLease final {
 public:
  MetalOutputLease() = default;
  MetalOutputLease(const MetalOutputLease&) noexcept;
  MetalOutputLease& operator=(const MetalOutputLease&) noexcept;
  MetalOutputLease(MetalOutputLease&&) noexcept;
  MetalOutputLease& operator=(MetalOutputLease&&) noexcept;
  ~MetalOutputLease();

  explicit operator bool() const noexcept { return slot_ != nullptr; }
  CVPixelBufferRef pixel_buffer() const noexcept;
  id<MTLTexture> texture() const noexcept;
  // Internal asynchronous consumers attach the renderer Impl here so the
  // output pool remains alive through routing, preview, or encoding.
  void anchor_lifetime(std::shared_ptr<void> owner) noexcept;

 private:
  friend class MetalOutputPool;
  explicit MetalOutputLease(MetalOutputSlot* slot) noexcept;
  void reset() noexcept;
  MetalOutputSlot* slot_{};
  std::shared_ptr<void> lifetime_anchor_;
};

// Lifetime contract: the pool and its shared MetalContext must outlive every
// lease. Destruction with outstanding references rejects the contract violation
// by terminating before any slot or native surface can be destroyed.
class MetalOutputPool final {
 public:
  MetalOutputPool(std::shared_ptr<MetalContext> context,
                  std::uint32_t capacity, std::uint32_t width,
                  std::uint32_t height);
  ~MetalOutputPool() noexcept;
  MetalOutputPool(const MetalOutputPool&) = delete;
  MetalOutputPool& operator=(const MetalOutputPool&) = delete;
  MetalOutputPool(MetalOutputPool&&) = delete;
  MetalOutputPool& operator=(MetalOutputPool&&) = delete;

  std::optional<MetalOutputLease> try_acquire() noexcept;
  std::uint32_t high_water() const noexcept;

 private:
  friend class MetalOutputLease;
  void release(MetalOutputSlot* slot) noexcept;

  std::shared_ptr<MetalContext> context_;
  std::uint32_t capacity_{};
  std::unique_ptr<MetalOutputSlot[]> slots_;
  std::atomic_uint32_t in_use_{0};
  std::atomic_uint32_t high_water_{0};
};

struct MetalRenderResult final {
  MetalOutputLease output;
  std::uint64_t gpu_start_ns = 0;
  std::uint64_t gpu_end_ns = 0;
  // Retained only so diagnostic callers can wait for and inspect this exact
  // submission. Production submission remains asynchronous.
  id<MTLCommandBuffer> diagnostic_command_buffer = nil;
};

}  // namespace swim::metal
