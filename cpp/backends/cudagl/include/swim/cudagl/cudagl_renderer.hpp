#pragma once

#include <swim/core/asset.hpp>
#include <swim/core/backend.hpp>
#include <swim/core/config.hpp>
#include <swim/core/metrics.hpp>
#include <swim/cudagl/cudagl_frame.hpp>
#include <swim/cudagl/gl_loader.hpp>

#include <cstdint>
#include <functional>
#include <memory>
#include <string>

namespace swim::cudagl {

// Delivered on the render thread after a composite finishes on the GPU. Carries
// the GL texture id of the finished output; the preview samples it.
using CudaGlCompletedOutputSink = std::function<void(GLuint /*output_texture*/)>;

// GL stitch renderer with CUDA interop for NV12 upload. Static geometry/weights
// upload once; per submit() it copies each camera's NV12 CUDA planes into
// CUDA-registered GL textures, draws six meshes with additive FP16 accumulation,
// then resolves to an RGBA8 output texture. Must run on the thread that owns the
// GL context (the render thread makes the shared context current).
class CudaGlStitchRenderer final {
 public:
  CudaGlStitchRenderer(std::shared_ptr<CudaGlContext> context,
                       const swim::core::RuntimeAsset& asset,
                       const swim::core::AppConfig& config,
                       swim::core::RuntimeCounters* metrics,
                       CudaGlCompletedOutputSink sink);
  ~CudaGlStitchRenderer();
  CudaGlStitchRenderer(const CudaGlStitchRenderer&) = delete;
  CudaGlStitchRenderer& operator=(const CudaGlStitchRenderer&) = delete;

  bool submit(const swim::core::RenderSnapshot& snapshot) noexcept;
  void drain();
  bool has_fatal_error() const noexcept;
  std::string fatal_error_message() const;

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace swim::cudagl
