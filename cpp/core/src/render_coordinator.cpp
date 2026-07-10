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

std::chrono::nanoseconds RenderCoordinator::cadence_offset(
    std::uint64_t tick_index, std::uint32_t fps_num,
    std::uint32_t fps_den) noexcept {
  if (fps_num == 0) {
    return std::chrono::nanoseconds::max();
  }
  const auto numerator =
      static_cast<std::uint64_t>(fps_den) * 1'000'000'000ULL;
  const auto whole = numerator / fps_num;
  const auto remainder = numerator % fps_num;
  const auto cycles = tick_index / fps_num;
  const auto within_cycle = tick_index % fps_num;
  const auto maximum = static_cast<std::uint64_t>(
      std::chrono::nanoseconds::max().count());
  if ((whole != 0 && tick_index > maximum / whole) ||
      (remainder != 0 && cycles > maximum / remainder)) {
    return std::chrono::nanoseconds::max();
  }
  const auto base = tick_index * whole;
  const auto cycle_fraction = cycles * remainder;
  const auto within_fraction = within_cycle * remainder / fps_num;
  if (base > maximum - cycle_fraction ||
      base + cycle_fraction > maximum - within_fraction) {
    return std::chrono::nanoseconds::max();
  }
  return std::chrono::nanoseconds{
      static_cast<std::chrono::nanoseconds::rep>(
          base + cycle_fraction + within_fraction)};
}

RenderSubmitResult RenderCoordinator::tick(Clock::time_point sampled_at) {
  if (first_tick_ == Clock::time_point{}) {
    first_tick_ = sampled_at;
  }

  for (std::size_t camera = 0; camera < kCameraCount; ++camera) {
    FrameLease newest;
    if (mailboxes_[camera].consume_latest(newest)) {
      const auto& next = newest.metadata();
      if (have_real_frames_[camera] &&
          last_real_generations_[camera] == next.decoder_generation &&
          next.sequence > last_real_sequences_[camera] &&
          next.sequence - last_real_sequences_[camera] > 1) {
          metrics_.overwritten.fetch_add(next.sequence -
                                             last_real_sequences_[camera] - 1,
                                         std::memory_order_relaxed);
      }
      last_real_generations_[camera] = next.decoder_generation;
      last_real_sequences_[camera] = next.sequence;
      have_real_frames_[camera] = true;
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

  for (std::size_t camera = 0; camera < kCameraCount; ++camera) {
    if (!fronts_[camera] || replacements_[camera]) {
      continue;
    }
    const auto arrived = fronts_[camera].metadata().arrived_at;
    if (arrived != Clock::time_point{} && sampled_at >= arrived) {
      metrics_.frame_age[camera].observe(
          std::chrono::duration_cast<std::chrono::milliseconds>(sampled_at -
                                                                 arrived));
    }
  }

  RenderSnapshot snapshot{fronts_, sampled_at};
  const auto result = renderer_.submit(snapshot);
  switch (result) {
    case RenderSubmitResult::accepted:
      metrics_.render_submissions.fetch_add(1, std::memory_order_relaxed);
      return result;
    case RenderSubmitResult::not_ready:
    case RenderSubmitResult::backpressure:
      metrics_.render_drops.fetch_add(1, std::memory_order_relaxed);
      return result;
    case RenderSubmitResult::fatal: {
      auto message = renderer_.last_error();
      if (message.empty()) {
        message = "renderer reported a fatal native error";
      }
      throw std::runtime_error(message);
    }
    case RenderSubmitResult::invalid:
      throw std::runtime_error("renderer rejected an invalid snapshot");
  }
  throw std::runtime_error("renderer returned an unknown submit result");
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
    const auto result = tick(Clock::now());
    const bool accepted = result == RenderSubmitResult::accepted;
    if (accepted && active_started == Clock::time_point{}) {
      active_started = Clock::now();
      cadence_epoch = active_started;
      if (config_.duration.count() > 0) {
        finish_at = active_started + config_.duration;
      }
      tick_index = 0;
    }
    if (config_.mode == RunMode::benchmark) {
      if (!accepted) {
        std::unique_lock lock(stop_mutex);
        stop_condition.wait_for(lock, token, std::chrono::milliseconds{1},
                                [] { return false; });
      }
      continue;
    }

    ++tick_index;
    const auto cadence_deadline =
        cadence_epoch + cadence_offset(tick_index, config_.fps_num,
                                       config_.fps_den);
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
