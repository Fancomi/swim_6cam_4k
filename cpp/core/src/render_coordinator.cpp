#include <swim/core/render_coordinator.hpp>

#include <algorithm>
#include <condition_variable>
#include <cstdint>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>

namespace swim::core {

RenderCoordinator::RenderCoordinator(Mailboxes& mailboxes,
                                     IRenderer& renderer,
                                     const AppConfig& config,
                                     RuntimeCounters& metrics) noexcept
    : mailboxes_(mailboxes),
      renderer_(renderer),
      config_(config),
      metrics_(metrics) {}

bool RenderCoordinator::tick(Clock::time_point sampled_at) {
  if (first_tick_ == Clock::time_point{}) {
    first_tick_ = sampled_at;
  }

  for (std::size_t camera = 0; camera < kCameraCount; ++camera) {
    FrameLease newest;
    if (mailboxes_[camera].consume_latest(newest)) {
      if (fronts_[camera] && !replacements_[camera]) {
        const auto& previous = fronts_[camera].metadata();
        const auto& next = newest.metadata();
        if (previous.decoder_generation == next.decoder_generation &&
            next.sequence > previous.sequence + 1) {
          metrics_.overwritten.fetch_add(next.sequence - previous.sequence - 1,
                                         std::memory_order_relaxed);
        }
      }
      auto arrived_at = newest.metadata().arrived_at;
      if (arrived_at == Clock::time_point{}) {
        arrived_at = sampled_at;
      }
      fronts_[camera] = std::move(newest);
      last_source_frames_[camera] = arrived_at;
      replacements_[camera] = false;
      continue;
    }

    if (fronts_[camera]) {
      metrics_.reused.fetch_add(1, std::memory_order_relaxed);
    }
    const auto age_origin = last_source_frames_[camera] == Clock::time_point{}
                                ? first_tick_
                                : last_source_frames_[camera];
    if (!replacements_[camera] && sampled_at - age_origin > config_.replace_after) {
      fronts_[camera] = renderer_.replacement_frame(
          static_cast<std::uint32_t>(camera));
      replacements_[camera] = static_cast<bool>(fronts_[camera]);
    }
  }

  RenderSnapshot snapshot{fronts_, sampled_at};
  if (renderer_.submit(snapshot)) {
    metrics_.render_submissions.fetch_add(1, std::memory_order_relaxed);
    return true;
  }
  metrics_.render_drops.fetch_add(1, std::memory_order_relaxed);
  if (renderer_.has_fatal_error()) {
    auto message = renderer_.last_error();
    if (message.empty()) {
      message = "renderer reported a fatal native error";
    }
    throw std::runtime_error(message);
  }
  return false;
}

void RenderCoordinator::run(std::stop_token token) {
  const auto run_started = Clock::now();
  auto cadence_epoch = run_started;
  auto active_started = Clock::time_point{};
  auto finish_at = Clock::time_point::max();
  const auto warmup_finish =
      config_.duration.count() == 0
          ? Clock::time_point::max()
          : run_started + config_.replace_after + config_.duration;
  std::condition_variable_any stop_condition;
  std::mutex stop_mutex;
  std::uint64_t tick_index = 0;

  while (!token.stop_requested() && Clock::now() < finish_at &&
         (active_started != Clock::time_point{} ||
          Clock::now() < warmup_finish)) {
    const bool accepted = tick(Clock::now());
    if (accepted && active_started == Clock::time_point{}) {
      active_started = Clock::now();
      cadence_epoch = active_started;
      if (config_.duration.count() > 0) {
        finish_at = active_started + config_.duration;
      }
      tick_index = 0;
    }
    ++tick_index;
    if (config_.mode == RunMode::benchmark) {
      if (!accepted) {
        std::unique_lock lock(stop_mutex);
        stop_condition.wait_for(lock, token, std::chrono::milliseconds{1},
                                [] { return false; });
      }
      continue;
    }

    // Derive each deadline from the common epoch. This avoids cumulative
    // rounding and drift for the 30000/1001 cadence.
    const auto numerator = static_cast<long double>(tick_index) *
                           static_cast<long double>(config_.fps_den) *
                           1'000'000'000.0L;
    const auto nanoseconds = static_cast<std::int64_t>(
        numerator / static_cast<long double>(config_.fps_num));
    const auto cadence_deadline =
        cadence_epoch + std::chrono::nanoseconds{nanoseconds};
    const auto deadline = std::min(cadence_deadline, finish_at);
    std::unique_lock lock(stop_mutex);
    stop_condition.wait_until(lock, token, deadline, [] { return false; });
  }

  if (active_started != Clock::time_point{}) {
    const auto active_elapsed =
        std::min(Clock::now(), finish_at) - active_started;
    metrics_.render_active_ns.store(
        static_cast<std::uint64_t>(std::chrono::duration_cast<
                                      std::chrono::nanoseconds>(active_elapsed)
                                      .count()),
        std::memory_order_relaxed);
  }
}

}  // namespace swim::core
