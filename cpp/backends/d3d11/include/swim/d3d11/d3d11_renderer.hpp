#pragma once

#include <swim/core/asset.hpp>
#include <swim/core/backend.hpp>
#include <swim/core/config.hpp>
#include <swim/core/metrics.hpp>
#include <swim/d3d11/d3d11_frame.hpp>

#include <array>
#include <functional>
#include <memory>
#include <string>

namespace swim::d3d11 {

// Delivered on the render thread right after a composite completes on the GPU.
// The lease keeps the output surface alive for preview/encode consumers.
using D3D11CompletedOutputSink = std::function<void(D3D11OutputLease)>;

// GPU stitch renderer: uploads static geometry and feather weights once, then
// composites six camera frames into one BGRA output per submit(). The immediate
// context is single-threaded, so submit() runs synchronously and completion is
// observed with an event query; the completed output is then handed to the sink.
class D3D11StitchRenderer final {
 public:
  D3D11StitchRenderer(std::shared_ptr<D3D11Context> context,
                      const swim::core::RuntimeAsset& asset,
                      const swim::core::AppConfig& config,
                      swim::core::RuntimeCounters* metrics = nullptr,
                      D3D11CompletedOutputSink completed_output_sink = {});
  ~D3D11StitchRenderer();
  D3D11StitchRenderer(const D3D11StitchRenderer&) = delete;
  D3D11StitchRenderer& operator=(const D3D11StitchRenderer&) = delete;

  bool submit(const swim::core::RenderSnapshot& snapshot) noexcept;
  void drain();
  bool has_fatal_error() const noexcept;
  std::string fatal_error_message() const;

 private:
  class Impl;
  std::shared_ptr<Impl> impl_;
};

}  // namespace swim::d3d11
