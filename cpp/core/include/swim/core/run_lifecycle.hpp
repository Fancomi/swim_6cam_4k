#pragma once

#include <swim/core/camera_capacity.hpp>

#include <array>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <limits>

namespace swim::core {

enum class SourceEofDisposition : std::uint8_t {
  normal_after_stop,
  normal_after_deadline,
  fatal_before_active,
  fatal_before_deadline,
  fatal_unbounded,
};

inline const char* source_eof_failure_message(
    SourceEofDisposition disposition) noexcept {
  switch (disposition) {
    case SourceEofDisposition::fatal_before_active:
      return "MP4 reached EOF before global render became active";
    case SourceEofDisposition::fatal_before_deadline:
      return "MP4 reached EOF before global render deadline";
    case SourceEofDisposition::fatal_unbounded:
      return "MP4 reached EOF before explicit stop in unbounded run";
    case SourceEofDisposition::normal_after_stop:
    case SourceEofDisposition::normal_after_deadline:
      return "";
  }
  return "MP4 reached EOF in an unknown lifecycle state";
}

// One process-wide render lifecycle shared by the coordinator and source
// lanes. The first accepted render establishes the only finite deadline.
class RunLifecycle final {
 public:
  using Clock = std::chrono::steady_clock;

  explicit RunLifecycle(Clock::duration duration) noexcept
      : duration_ns_(std::max<std::int64_t>(
            0, std::chrono::duration_cast<std::chrono::nanoseconds>(duration)
                   .count())) {}

  bool mark_active(Clock::time_point started_at) noexcept {
    if (stop_requested()) {
      return false;
    }
    std::uint8_t expected = 0;
    if (!state_.compare_exchange_strong(expected, 1,
                                        std::memory_order_acq_rel,
                                        std::memory_order_acquire)) {
      return false;
    }
    const auto start_ns = to_nanoseconds(started_at);
    const auto maximum = std::numeric_limits<std::int64_t>::max();
    const auto deadline =
        duration_ns_ == 0
            ? maximum
            : (start_ns > maximum - duration_ns_ ? maximum
                                                  : start_ns + duration_ns_);
    started_ns_.store(start_ns, std::memory_order_relaxed);
    deadline_ns_.store(deadline, std::memory_order_relaxed);
    state_.store(2, std::memory_order_release);
    return true;
  }

  bool active() const noexcept {
    return state_.load(std::memory_order_acquire) == 2;
  }

  bool finite() const noexcept { return duration_ns_ != 0; }

  Clock::time_point active_started_at() const noexcept {
    if (!active()) {
      return {};
    }
    return Clock::time_point{
        std::chrono::nanoseconds{started_ns_.load(std::memory_order_acquire)}};
  }

  Clock::time_point deadline() const noexcept {
    if (!finite() || !active()) {
      return Clock::time_point::max();
    }
    return Clock::time_point{
        std::chrono::nanoseconds{deadline_ns_.load(std::memory_order_acquire)}};
  }

  void request_stop() noexcept {
    stop_requested_.store(true, std::memory_order_release);
  }

  bool stop_requested() const noexcept {
    return stop_requested_.load(std::memory_order_acquire);
  }

  bool deadline_reached(Clock::time_point now) const noexcept {
    return finite() && active() && now >= deadline();
  }

  bool should_stop(Clock::time_point now) const noexcept {
    return stop_requested() || deadline_reached(now);
  }

  SourceEofDisposition classify_eof(Clock::time_point now) const noexcept {
    if (stop_requested()) {
      return SourceEofDisposition::normal_after_stop;
    }
    if (!active()) {
      return SourceEofDisposition::fatal_before_active;
    }
    if (!finite()) {
      return SourceEofDisposition::fatal_unbounded;
    }
    return deadline_reached(now)
               ? SourceEofDisposition::normal_after_deadline
               : SourceEofDisposition::fatal_before_deadline;
  }

 private:
  static std::int64_t to_nanoseconds(Clock::time_point value) noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               value.time_since_epoch())
        .count();
  }

  const std::int64_t duration_ns_;
  std::atomic_uint8_t state_{0};  // inactive, initializing, active
  std::atomic_int64_t started_ns_{0};
  std::atomic_int64_t deadline_ns_{std::numeric_limits<std::int64_t>::max()};
  std::atomic_bool stop_requested_{false};
};

class RuntimeStartState final {
 public:
  void mark_started(std::size_t camera) noexcept {
    if (camera < started_.size()) {
      started_[camera] = true;
    }
  }

  bool started(std::size_t camera) const noexcept {
    return camera < started_.size() && started_[camera];
  }

  std::size_t started_count() const noexcept {
    std::size_t result = 0;
    for (const auto value : started_) {
      result += value ? 1U : 0U;
    }
    return result;
  }

  std::size_t healthy_count(const std::array<bool, kMaxCameras>& failed) const noexcept {
    std::size_t result = 0;
    for (std::size_t camera = 0; camera < started_.size(); ++camera) {
      result += started_[camera] && !failed[camera] ? 1U : 0U;
    }
    return result;
  }

 private:
  std::array<bool, kMaxCameras> started_{};
};

}  // namespace swim::core
