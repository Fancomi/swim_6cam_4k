#pragma once

#include <swim/core/backend.hpp>
#include <swim/core/config.hpp>
#include <swim/core/latest_frame_mailbox.hpp>
#include <swim/core/metrics.hpp>

#include <array>
#include <chrono>
#include <cstddef>
#include <stop_token>

namespace swim::core {

// Samples each latest-value lane independently. The coordinator never waits
// for a camera and retains the six selected front leases between ticks.
class RenderCoordinator final {
 public:
  static constexpr std::size_t kCameraCount = 6;
  using Clock = std::chrono::steady_clock;
  using Mailboxes = std::array<LatestFrameMailbox, kCameraCount>;

  RenderCoordinator(Mailboxes& mailboxes, IRenderer& renderer,
                    const AppConfig& config, RuntimeCounters& metrics) noexcept;

  // Performs exactly one consume attempt per lane and one renderer submit.
  // Returns the renderer's acceptance result.
  bool tick(Clock::time_point sampled_at);
  void run(std::stop_token token);

 private:
  Mailboxes& mailboxes_;
  IRenderer& renderer_;
  const AppConfig& config_;
  RuntimeCounters& metrics_;
  std::array<FrameLease, kCameraCount> fronts_;
  std::array<Clock::time_point, kCameraCount> last_source_frames_{};
  std::array<bool, kCameraCount> replacements_{};
  Clock::time_point first_tick_{};
};

}  // namespace swim::core
