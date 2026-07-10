#pragma once

#include <swim/core/asset.hpp>
#include <swim/core/backend.hpp>
#include <swim/core/config.hpp>
#include <swim/metal/metal_frame.hpp>

#include <array>
#include <memory>

namespace swim::metal {

inline constexpr std::uint32_t kMetalFrameBackendTag = 0x4D544C31U;

class MetalStitchRenderer final {
 public:
  MetalStitchRenderer(std::shared_ptr<MetalContext> context,
                      const swim::core::RuntimeAsset& asset,
                      const swim::core::AppConfig& config);
  ~MetalStitchRenderer();
  MetalStitchRenderer(const MetalStitchRenderer&) = delete;
  MetalStitchRenderer& operator=(const MetalStitchRenderer&) = delete;

  bool submit(const std::array<MetalFrameView, 6>& frames,
              MetalRenderResult& result) noexcept;
  bool submit(const swim::core::RenderSnapshot& snapshot,
              MetalRenderResult& result) noexcept;

  // Diagnostic-only synchronization. Production submission never waits or
  // reads output pixels on the CPU.
  void wait_for_completion(MetalRenderResult& result);
  void drain();

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace swim::metal
