#include <swim/core/camera_health.hpp>

#include <array>
#include <chrono>

namespace swim::core {

CameraHealthTracker::CameraHealthTracker(
    std::chrono::milliseconds stale_after,
    std::chrono::milliseconds reconnect_after) noexcept
    : stale_after_(stale_after), reconnect_after_(reconnect_after) {}

void CameraHealthTracker::on_frame(Clock::time_point received_at) noexcept {
  if (state_ == CameraState::failed) {
    return;
  }
  last_frame_ = received_at;
  has_frame_ = true;
  state_ = CameraState::healthy;
  reconnect_attempt_ = 0;
}

void CameraHealthTracker::on_unrecoverable_error() noexcept {
  state_ = CameraState::failed;
}

CameraState CameraHealthTracker::tick(Clock::time_point now) noexcept {
  if (state_ == CameraState::failed) {
    return state_;
  }
  if (!has_frame_) {
    return state_;
  }

  const auto frame_age = now - last_frame_;
  if (frame_age >= reconnect_after_) {
    state_ = CameraState::reconnecting;
  } else if (frame_age >= stale_after_) {
    state_ = CameraState::stale;
  } else {
    state_ = CameraState::healthy;
  }
  return state_;
}

std::chrono::milliseconds CameraHealthTracker::next_reconnect_delay() noexcept {
  using namespace std::chrono_literals;
  static constexpr std::array delays{250ms, 500ms, 1000ms,
                                     2000ms, 4000ms, 5000ms};

  const auto delay = delays[reconnect_attempt_];
  if (reconnect_attempt_ + 1U < delays.size()) {
    ++reconnect_attempt_;
  }
  return delay;
}

CameraState CameraHealthTracker::state() const noexcept { return state_; }

}  // namespace swim::core
