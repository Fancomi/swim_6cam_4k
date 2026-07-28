#pragma once

#include <swim/core/config.hpp>
#include <swim/core/latest_frame_mailbox.hpp>
#include <swim/core/metrics.hpp>
#include <swim/core/run_lifecycle.hpp>
#include <swim/d3d11/d3d11_frame.hpp>

#include <cstdint>
#include <memory>
#include <string>

namespace swim::d3d11 {

// One lane-local Media Foundation hardware-decode reader. IMFSourceReader is
// bound to the shared D3D11 device through an IMFDXGIDeviceManager so decoded
// NV12 frames stay on the GPU as ID3D11Texture2D. The caller owns the mailbox
// and counters and must keep them alive until wait() returns.
class MfSource final {
 public:
  MfSource(std::shared_ptr<D3D11Context> context,
           swim::core::SourceConfig source, std::uint32_t camera_index,
           swim::core::LatestFrameMailbox& mailbox,
           swim::core::RuntimeCounters& counters,
           swim::core::RunMode mode, std::uint32_t ticket_capacity,
           std::uint32_t surface_capacity,
           swim::core::RunLifecycle* lifecycle);
  ~MfSource();

  MfSource(const MfSource&) = delete;
  MfSource& operator=(const MfSource&) = delete;

  void start();
  void stop() noexcept;
  void wait();

  bool running() const noexcept;
  bool failed() const noexcept;
  std::string last_error() const;

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace swim::d3d11
