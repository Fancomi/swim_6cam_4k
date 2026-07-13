#pragma once

#include <chrono>
#include <cstdint>

namespace swim::core {

enum class CameraState : std::uint8_t {
  starting,
  healthy,
  stale,
  reconnecting,
  failed,
};

class CameraHealthTracker {
 public:
  using Clock = std::chrono::steady_clock;

  explicit CameraHealthTracker(
      std::chrono::milliseconds stale_after = std::chrono::milliseconds{100},
      std::chrono::milliseconds reconnect_after =
          std::chrono::milliseconds{1000}) noexcept;

  void on_frame(Clock::time_point received_at) noexcept;
  void on_unrecoverable_error() noexcept;
  CameraState tick(Clock::time_point now) noexcept;
  std::chrono::milliseconds next_reconnect_delay() noexcept;
  CameraState state() const noexcept;

 private:
  CameraState state_{CameraState::starting};
  Clock::time_point last_frame_{};
  std::chrono::milliseconds stale_after_;
  std::chrono::milliseconds reconnect_after_;
  bool has_frame_{};
  std::uint8_t reconnect_attempt_{};
};

}  // namespace swim::core
