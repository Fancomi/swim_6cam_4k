#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>

namespace swim::core {

// 128 bytes covers Apple Silicon's cache line and remains conservative on
// common 64-byte-line targets without introducing platform headers.
inline constexpr std::size_t kMetricsCacheLineBytes = 128;

class FixedLatencyHistogramSnapshot {
 public:
  // Finite quantiles are clamped to [0, 1]. Non-finite values throw
  // std::invalid_argument.
  std::chrono::milliseconds percentile(double quantile) const;
  std::uint64_t count() const noexcept;

 private:
  friend class FixedLatencyHistogram;

  explicit FixedLatencyHistogramSnapshot(
      const std::array<std::uint64_t, 1001>& buckets,
      std::uint64_t count) noexcept;

  std::array<std::uint64_t, 1001> buckets_{};
  std::uint64_t count_{};
};

class FixedLatencyHistogram {
 public:
  static constexpr std::size_t kMaximumMilliseconds = 1000;
  static constexpr std::size_t kBucketCount = kMaximumMilliseconds + 1;

  void observe(std::chrono::milliseconds latency) noexcept;
  // Finite quantiles are clamped to [0, 1]. Non-finite values throw
  // std::invalid_argument.
  std::chrono::milliseconds percentile(double quantile) const;
  FixedLatencyHistogramSnapshot sample() const noexcept;
  FixedLatencyHistogramSnapshot snapshot_and_reset() noexcept;
  std::uint64_t count() const noexcept;

 private:
  std::array<std::atomic_uint64_t, kBucketCount> buckets_{};
};

struct MetricsSnapshot final {
  const std::uint64_t received;
  const std::uint64_t decoded;
  const std::uint64_t published;
  const std::uint64_t overwritten;
  const std::uint64_t reused;
  const std::uint64_t malformed;
  const std::uint64_t reconnects;
  const std::uint64_t render_submissions;
  const std::uint64_t render_completions;
  const std::uint64_t render_drops;
  const std::uint64_t render_active_ns;
  const std::uint64_t render_first_submit_ns;
  const std::uint64_t render_last_completion_ns;
  const std::uint64_t render_inflight_capacity;
  const std::uint64_t render_inflight_in_use;
  const std::uint64_t render_inflight_high_water;
  const std::uint64_t render_inflight_pool_misses;
  const std::uint64_t render_output_capacity;
  const std::uint64_t render_output_in_use;
  const std::uint64_t render_output_high_water;
  const std::uint64_t render_output_pool_misses;
  const std::array<std::uint64_t, 6> camera_received;
  const std::array<std::uint64_t, 6> camera_decoded;
  const std::array<std::uint64_t, 6> camera_published;
  const std::array<std::uint64_t, 6> camera_overwritten;
  const std::array<std::uint64_t, 6> camera_reused;
  const std::array<std::uint64_t, 6> frame_age_ms_p50;
  const std::array<std::uint64_t, 6> frame_age_ms_p95;
  const std::array<std::uint64_t, 6> frame_age_ms_p99;
  const std::uint64_t snapshot_age_spread_ms_p99;
  const std::uint64_t gpu_render_duration_ms_p50;
  const std::uint64_t gpu_render_duration_ms_p95;
  const std::uint64_t preview_submissions;
  const std::uint64_t preview_completions;
  const std::uint64_t preview_drops;
  const std::uint64_t preview_presents;
  const std::uint64_t encode_submissions;
  const std::uint64_t encode_completions;
  const std::uint64_t encode_bytes;
  const std::uint64_t encode_drops;
  const std::uint64_t encode_rejected_frames;
  const std::uint64_t encode_callback_errors;
  const std::uint64_t encode_first_submit_ns;
  const std::uint64_t encode_last_completion_ns;
  const std::uint64_t encode_input_capacity;
  const std::uint64_t encode_input_in_use;
  const std::uint64_t encode_input_high_water;
  const std::uint64_t encode_input_pool_misses;
  const bool encode_using_hardware;
  const std::uint64_t encode_drain_timeouts;
  const std::uint64_t decoded_pixel_host_copies;
  const std::uint64_t pool_exhaustion;
  const std::uint64_t native_texture_wrappers;
  const std::uint64_t native_command_buffers;
  const std::uint64_t native_decode_tickets;
  const std::uint64_t native_callback_wrappers;
  const std::uint64_t application_hot_path_allocations;
  const std::array<std::uint64_t, 6> decode_surface_capacity;
  const std::array<std::uint64_t, 6> decode_surface_in_use;
  const std::array<std::uint64_t, 6> decode_surface_high_water;
  const std::array<std::uint64_t, 6> decode_surface_pool_misses;
  const std::array<std::uint64_t, 6> decode_ticket_capacity;
  const std::array<std::uint64_t, 6> decode_ticket_in_use;
  const std::array<std::uint64_t, 6> decode_ticket_high_water;
  const std::array<std::uint64_t, 6> decode_ticket_pool_misses;

  std::uint64_t render_completion_interval_ns() const noexcept;
  double render_completion_fps() const noexcept;
  std::uint64_t encode_completion_interval_ns() const noexcept;
  double encode_completion_fps() const noexcept;
};

struct RuntimeCounters final {
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t received{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t decoded{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t published{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t overwritten{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t reused{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t malformed{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t reconnects{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_submissions{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_completions{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_drops{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_active_ns{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_first_submit_ns{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_last_completion_ns{};
  // One runtime renderer writes these startup/final gauges. They are exchanged
  // only by the single final snapshot after renderer drain.
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_inflight_capacity{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_inflight_in_use{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_inflight_high_water{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_inflight_pool_misses{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_output_capacity{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_output_in_use{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_output_high_water{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_output_pool_misses{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t preview_submissions{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t preview_completions{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t preview_drops{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t preview_presents{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_submissions{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_completions{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_bytes{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_drops{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_rejected_frames{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_callback_errors{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_first_submit_ns{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t
      encode_last_completion_ns{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_input_capacity{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_input_in_use{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t
      encode_input_high_water{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t
      encode_input_pool_misses{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_using_hardware{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_drain_timeouts{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t
      decoded_pixel_host_copies{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t pool_exhaustion{};

  // Generic native-object creation counters. Platform backends map their
  // framework-specific wrappers onto these categories.
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t
      native_texture_wrappers{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t native_command_buffers{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t native_decode_tickets{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t
      native_callback_wrappers{};

  std::array<std::atomic_uint64_t, 6> camera_received{};
  std::array<std::atomic_uint64_t, 6> camera_decoded{};
  std::array<std::atomic_uint64_t, 6> camera_published{};
  std::array<std::atomic_uint64_t, 6> camera_overwritten{};
  std::array<std::atomic_uint64_t, 6> camera_reused{};
  std::array<std::atomic_uint64_t, 6> decode_surface_capacity{};
  std::array<std::atomic_uint64_t, 6> decode_surface_in_use{};
  std::array<std::atomic_uint64_t, 6> decode_surface_high_water{};
  std::array<std::atomic_uint64_t, 6> decode_surface_pool_misses{};
  std::array<std::atomic_uint64_t, 6> decode_ticket_capacity{};
  std::array<std::atomic_uint64_t, 6> decode_ticket_in_use{};
  std::array<std::atomic_uint64_t, 6> decode_ticket_high_water{};
  std::array<std::atomic_uint64_t, 6> decode_ticket_pool_misses{};

  // Fixed atomic buckets make reporter snapshots race-safe without handoff
  // allocation or destructive reset.
  std::array<FixedLatencyHistogram, 6> frame_age;
  FixedLatencyHistogram snapshot_age_spread;
  FixedLatencyHistogram gpu_render_duration;

  MetricsSnapshot sample_totals() const noexcept;
  MetricsSnapshot snapshot_and_reset() noexcept;
};

std::uint64_t monotonic_delta(std::uint64_t current,
                              std::uint64_t previous) noexcept;

static_assert(sizeof(std::atomic_uint64_t) <= kMetricsCacheLineBytes);

}  // namespace swim::core
