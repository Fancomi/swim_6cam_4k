#pragma once

#include <swim/core/metrics.hpp>
#include <swim/d3d11/d3d11_frame.hpp>

#include <functional>
#include <memory>
#include <stop_token>

namespace swim::d3d11 {

// Win32 window + DXGI swap chain presenter. offer() is a non-blocking producer
// entry point invoked from the render thread; all window and Present work stays
// inside run_main_loop() on the process main thread, mirroring MetalPreview.
class D3D11Preview final {
 public:
  using CloseCallback = std::function<void()>;

  D3D11Preview(std::shared_ptr<D3D11Context> context, std::uint32_t width,
               std::uint32_t height, swim::core::RuntimeCounters& metrics,
               CloseCallback close_callback, bool visible = true);
  ~D3D11Preview();
  D3D11Preview(const D3D11Preview&) = delete;
  D3D11Preview& operator=(const D3D11Preview&) = delete;

  bool offer(D3D11OutputLease output) noexcept;
  void run_main_loop(std::stop_token token);
  void request_stop() noexcept;

 private:
  class Impl;
  std::shared_ptr<Impl> impl_;
};

}  // namespace swim::d3d11
