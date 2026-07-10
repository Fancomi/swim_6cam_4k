#pragma once

#include <swim/core/config.hpp>
#include <swim/core/latest_frame_mailbox.hpp>
#include <swim/core/metrics.hpp>
#include <swim/metal/metal_frame.hpp>
#include <swim/metal/videotoolbox_decoder.hpp>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <memory>
#include <string>

namespace swim::metal {

// One lane-local compressed MP4 reader and VideoToolbox decoder.  The caller
// owns the mailbox and counters and must keep them alive until wait() returns.
class Mp4VideoToolboxSource final {
 public:
  Mp4VideoToolboxSource(
      std::shared_ptr<MetalContext> context,
      swim::core::SourceConfig source, std::uint32_t camera_index,
      swim::core::LatestFrameMailbox& mailbox,
      swim::core::RuntimeCounters& counters,
      swim::core::RunMode mode = swim::core::RunMode::benchmark,
      std::chrono::milliseconds run_duration = std::chrono::milliseconds{0},
      std::uint32_t ticket_capacity = 16,
      std::uint32_t surface_capacity = 8);
  ~Mp4VideoToolboxSource();

  Mp4VideoToolboxSource(const Mp4VideoToolboxSource&) = delete;
  Mp4VideoToolboxSource& operator=(const Mp4VideoToolboxSource&) = delete;

  void start();
  void stop() noexcept;
  void wait();

  bool running() const noexcept;
  bool failed() const noexcept;
  bool using_hardware_acceleration() const noexcept;
  std::uint64_t decoder_generation() const noexcept;
  VideoToolboxDecoderStats decoder_stats() const;
  std::string last_error() const;

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace swim::metal
