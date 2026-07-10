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
  FixedLatencyHistogramSnapshot snapshot_and_reset() noexcept;
  std::uint64_t count() const noexcept;

 private:
  std::array<std::uint64_t, kBucketCount> buckets_{};
  std::uint64_t count_{};
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
  const std::uint64_t render_inflight_high_water;
  const std::uint64_t render_inflight_pool_misses;
  const std::uint64_t render_output_capacity;
  const std::uint64_t render_output_high_water;
  const std::uint64_t render_output_pool_misses;
  const std::array<std::uint64_t, 6> frame_age_ms_p99;
  const std::uint64_t preview_drops;
  const std::uint64_t encode_drops;
  const std::uint64_t decoded_pixel_host_copies;
  const std::uint64_t pool_exhaustion;
  const std::uint64_t native_texture_wrappers;
  const std::uint64_t native_command_buffers;
  const std::uint64_t native_decode_tickets;
  const std::uint64_t native_callback_wrappers;

  std::uint64_t render_completion_interval_ns() const noexcept;
  double render_completion_fps() const noexcept;
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
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_inflight_high_water{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_inflight_pool_misses{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_output_capacity{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_output_high_water{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t render_output_pool_misses{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t preview_drops{};
  alignas(kMetricsCacheLineBytes) std::atomic_uint64_t encode_drops{};
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

  // Written only by the render thread and snapshotted after that thread joins.
  std::array<FixedLatencyHistogram, 6> frame_age;

  MetricsSnapshot snapshot_and_reset() noexcept;
};

static_assert(sizeof(std::atomic_uint64_t) <= kMetricsCacheLineBytes);

}  // namespace swim::core
