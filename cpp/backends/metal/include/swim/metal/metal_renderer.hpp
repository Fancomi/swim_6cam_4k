#pragma once

#include <swim/core/asset.hpp>
#include <swim/core/backend.hpp>
#include <swim/core/config.hpp>
#include <swim/metal/metal_frame.hpp>

#include <array>
#include <functional>
#include <memory>
#include <string>

namespace swim::metal {

inline constexpr std::uint32_t kMetalFrameBackendTag = 0x4D544C31U;

using MetalCompletedOutputSink = std::function<void(MetalOutputLease)>;

// Completion callbacks can arrive on arbitrary Metal threads. This router
// turns them into one serial producer before any bounded downstream handoff.
class MetalCompletedOutputRouter final {
 public:
  MetalCompletedOutputRouter();
  ~MetalCompletedOutputRouter();
  MetalCompletedOutputRouter(const MetalCompletedOutputRouter&) = delete;
  MetalCompletedOutputRouter& operator=(const MetalCompletedOutputRouter&) =
      delete;

  // Sinks are registered during startup, before route() is called.
  void add_sink(MetalCompletedOutputSink sink);
  bool route(MetalOutputLease output) noexcept;
  // Terminal operation. Rejects new routes and waits for accepted deliveries.
  void close_and_flush();

 private:
  class Impl;
  std::shared_ptr<Impl> impl_;
};

class MetalStitchRenderer final {
 public:
  MetalStitchRenderer(std::shared_ptr<MetalContext> context,
                      const swim::core::RuntimeAsset& asset,
                      const swim::core::AppConfig& config,
                      swim::core::RuntimeCounters* metrics = nullptr,
                      MetalCompletedOutputSink completed_output_sink = {});
  ~MetalStitchRenderer();
  MetalStitchRenderer(const MetalStitchRenderer&) = delete;
  MetalStitchRenderer& operator=(const MetalStitchRenderer&) = delete;

  bool submit(const std::array<MetalFrameView, swim::core::kMaxCameras>& frames,
              MetalRenderResult& result) noexcept;
  bool submit(const swim::core::RenderSnapshot& snapshot,
              MetalRenderResult& result) noexcept;

  // Diagnostic-only synchronization. Production submission never waits or
  // reads output pixels on the CPU.
  void wait_for_completion(MetalRenderResult& result);
  // Terminal shutdown operation. Submissions after drain begins are rejected.
  void drain();
  bool has_fatal_error() const noexcept;
  std::string fatal_error_message() const;

  // Diagnostic-only startup contract checks.
  bool uploaded_vertices_match(
      const swim::core::RuntimeAsset& asset) const noexcept;
  float raster_position_expansion() const noexcept;

 private:
  class Impl;
  std::shared_ptr<Impl> impl_;
};

}  // namespace swim::metal
