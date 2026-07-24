#include "test_support.hpp"

#include <swim/core/camera_health.hpp>

#include <chrono>

using namespace std::chrono_literals;

TEST_CASE(camera_becomes_stale_then_reconnecting_at_exact_thresholds) {
  using swim::core::CameraHealthTracker;
  using swim::core::CameraState;

  const auto t0 = std::chrono::steady_clock::time_point{};
  CameraHealthTracker health;

  CHECK_EQ(health.state(), CameraState::starting);
  health.on_frame(t0);
  CHECK_EQ(health.tick(t0 + 99ms), CameraState::healthy);
  CHECK_EQ(health.tick(t0 + 100ms), CameraState::stale);
  CHECK_EQ(health.tick(t0 + 999ms), CameraState::stale);
  CHECK_EQ(health.tick(t0 + 1000ms), CameraState::reconnecting);
}

TEST_CASE(camera_health_uses_configured_thresholds) {
  using swim::core::CameraHealthTracker;
  using swim::core::CameraState;

  const auto t0 = std::chrono::steady_clock::time_point{};
  CameraHealthTracker health{25ms, 250ms};

  health.on_frame(t0);
  CHECK_EQ(health.tick(t0 + 24ms), CameraState::healthy);
  CHECK_EQ(health.tick(t0 + 25ms), CameraState::stale);
  CHECK_EQ(health.tick(t0 + 250ms), CameraState::reconnecting);
}

TEST_CASE(reconnect_backoff_caps_and_resets_after_a_healthy_frame) {
  using swim::core::CameraHealthTracker;
  using swim::core::CameraState;

  const auto t0 = std::chrono::steady_clock::time_point{};
  CameraHealthTracker health;

  health.on_frame(t0);
  CHECK_EQ(health.tick(t0 + 1000ms), CameraState::reconnecting);
  CHECK_EQ(health.next_reconnect_delay(), 250ms);
  CHECK_EQ(health.next_reconnect_delay(), 500ms);
  CHECK_EQ(health.next_reconnect_delay(), 1000ms);
  CHECK_EQ(health.next_reconnect_delay(), 2000ms);
  CHECK_EQ(health.next_reconnect_delay(), 4000ms);
  CHECK_EQ(health.next_reconnect_delay(), 5000ms);
  CHECK_EQ(health.next_reconnect_delay(), 5000ms);

  health.on_frame(t0 + 2000ms);
  CHECK_EQ(health.state(), CameraState::healthy);
  CHECK_EQ(health.next_reconnect_delay(), 250ms);
}

TEST_CASE(unrecoverable_failure_is_terminal_for_tracker_lifetime) {
  using swim::core::CameraHealthTracker;
  using swim::core::CameraState;

  const auto t0 = std::chrono::steady_clock::time_point{};
  CameraHealthTracker health;

  CHECK_EQ(health.tick(t0), CameraState::starting);
  health.on_unrecoverable_error();
  CHECK_EQ(health.state(), CameraState::failed);
  health.on_frame(t0 + 1ms);
  CHECK_EQ(health.state(), CameraState::failed);
  CHECK_EQ(health.tick(t0 + 2000ms), CameraState::failed);
}
