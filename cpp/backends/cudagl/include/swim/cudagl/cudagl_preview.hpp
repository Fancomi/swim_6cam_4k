#pragma once

#include <swim/core/metrics.hpp>
#include <swim/cudagl/cudagl_frame.hpp>
#include <swim/cudagl/gl_loader.hpp>

#include <cstdint>
#include <functional>
#include <memory>
#include <stop_token>

namespace swim::cudagl {

// GLFW window that presents the latest composite GL texture. Shares the GL
// context created by the backend (the render thread owns current-context; the
// preview window shares resources so it can sample the output texture). offer()
// records the latest output texture id; the main loop draws it and swaps.
class CudaGlPreview final {
 public:
  using CloseCallback = std::function<void()>;

  CudaGlPreview(std::shared_ptr<CudaGlContext> context, std::uint32_t width,
                std::uint32_t height, swim::core::RuntimeCounters& metrics,
                CloseCallback close_callback, bool visible);
  ~CudaGlPreview();
  CudaGlPreview(const CudaGlPreview&) = delete;
  CudaGlPreview& operator=(const CudaGlPreview&) = delete;

  // Records the latest finished output GL texture (called on the render thread).
  void offer(GLuint output_texture) noexcept;
  void run_main_loop(std::stop_token token);
  void request_stop() noexcept;

 private:
  class Impl;
  std::shared_ptr<Impl> impl_;
};

}  // namespace swim::cudagl
