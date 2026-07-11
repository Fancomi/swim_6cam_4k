#pragma once

#include <swim/core/backend.hpp>
#include <swim/core/benchmark_stage.hpp>
#include <swim/core/config.hpp>
#include <swim/core/latest_frame_mailbox.hpp>
#include <swim/core/metrics.hpp>
#include <swim/core/run_lifecycle.hpp>

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
  using Mailboxes = MailboxArray;

  RenderCoordinator(Mailboxes& mailboxes, IRenderer& renderer,
                    const AppConfig& config, BenchmarkGraph graph,
                    RuntimeCounters& metrics, RunLifecycle& lifecycle);

  // Performs exactly one consume attempt per lane and one renderer submit.
  // Returns the renderer's acceptance result.
  RenderSubmitResult tick(Clock::time_point sampled_at);
  void run(std::stop_token token);
  static std::chrono::nanoseconds cadence_offset(
      std::uint64_t tick_index, std::uint32_t fps_num,
      std::uint32_t fps_den) noexcept;

 private:
  Mailboxes& mailboxes_;
  IRenderer& renderer_;
  const AppConfig& config_;
  BenchmarkGraph graph_;
  RuntimeCounters& metrics_;
  RunLifecycle& lifecycle_;
  std::array<FrameLease, kCameraCount> fronts_;
  std::array<Clock::time_point, kCameraCount> last_source_frames_{};
  std::array<bool, kCameraCount> replacements_{};
  std::array<bool, kCameraCount> fixed_frames_{};
  std::array<std::uint64_t, kCameraCount> last_real_generations_{};
  std::array<std::uint64_t, kCameraCount> last_real_sequences_{};
  std::array<bool, kCameraCount> have_real_frames_{};
  Clock::time_point first_tick_{};
};

}  // namespace swim::core
