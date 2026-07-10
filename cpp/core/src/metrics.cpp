#include <swim/core/metrics.hpp>

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace swim::core {

namespace {

template <std::size_t Size>
std::chrono::milliseconds percentile_of(
    const std::array<std::uint64_t, Size>& buckets,
    std::uint64_t count,
    double quantile) {
  if (!std::isfinite(quantile)) {
    throw std::invalid_argument("histogram quantile must be finite");
  }
  if (count == 0) {
    return std::chrono::milliseconds{0};
  }

  const auto bounded_quantile = std::clamp(quantile, 0.0, 1.0);
  const auto requested_rank = static_cast<std::uint64_t>(
      std::ceil(bounded_quantile * static_cast<double>(count)));
  const auto rank = std::max<std::uint64_t>(requested_rank, 1);

  std::uint64_t cumulative = 0;
  for (std::size_t bucket = 0; bucket < buckets.size(); ++bucket) {
    cumulative += buckets[bucket];
    if (cumulative >= rank) {
      return std::chrono::milliseconds{
          static_cast<std::chrono::milliseconds::rep>(bucket)};
    }
  }

  return std::chrono::milliseconds{
      static_cast<std::chrono::milliseconds::rep>(buckets.size() - 1)};
}

}  // namespace

std::uint64_t MetricsSnapshot::render_completion_interval_ns() const noexcept {
  return render_last_completion_ns >= render_first_submit_ns
             ? render_last_completion_ns - render_first_submit_ns
             : 0;
}

double MetricsSnapshot::render_completion_fps() const noexcept {
  const auto interval = render_completion_interval_ns();
  return interval == 0
             ? 0.0
             : static_cast<double>(render_completions) * 1'000'000'000.0 /
                   static_cast<double>(interval);
}

FixedLatencyHistogramSnapshot::FixedLatencyHistogramSnapshot(
    const std::array<std::uint64_t, 1001>& buckets,
    std::uint64_t count) noexcept
    : buckets_(buckets), count_(count) {}

std::chrono::milliseconds FixedLatencyHistogramSnapshot::percentile(
    double quantile) const {
  return percentile_of(buckets_, count_, quantile);
}

std::uint64_t FixedLatencyHistogramSnapshot::count() const noexcept {
  return count_;
}

void FixedLatencyHistogram::observe(
    std::chrono::milliseconds latency) noexcept {
  const auto bucket = std::clamp<std::chrono::milliseconds::rep>(
      latency.count(), 0,
      static_cast<std::chrono::milliseconds::rep>(kMaximumMilliseconds));
  ++buckets_[static_cast<std::size_t>(bucket)];
  ++count_;
}

std::chrono::milliseconds FixedLatencyHistogram::percentile(
    double quantile) const {
  return percentile_of(buckets_, count_, quantile);
}

FixedLatencyHistogramSnapshot FixedLatencyHistogram::snapshot_and_reset()
    noexcept {
  FixedLatencyHistogramSnapshot snapshot{buckets_, count_};
  buckets_.fill(0);
  count_ = 0;
  return snapshot;
}

std::uint64_t FixedLatencyHistogram::count() const noexcept { return count_; }

MetricsSnapshot RuntimeCounters::snapshot_and_reset() noexcept {
  std::array<std::uint64_t, 6> frame_age_p99{};
  for (std::size_t camera = 0; camera < frame_age.size(); ++camera) {
    frame_age_p99[camera] = static_cast<std::uint64_t>(
        frame_age[camera].snapshot_and_reset().percentile(0.99).count());
  }
  return MetricsSnapshot{
      received.exchange(0, std::memory_order_relaxed),
      decoded.exchange(0, std::memory_order_relaxed),
      published.exchange(0, std::memory_order_relaxed),
      overwritten.exchange(0, std::memory_order_relaxed),
      reused.exchange(0, std::memory_order_relaxed),
      malformed.exchange(0, std::memory_order_relaxed),
      reconnects.exchange(0, std::memory_order_relaxed),
      render_submissions.exchange(0, std::memory_order_relaxed),
      render_completions.exchange(0, std::memory_order_relaxed),
      render_drops.exchange(0, std::memory_order_relaxed),
      render_active_ns.exchange(0, std::memory_order_relaxed),
      render_first_submit_ns.exchange(0, std::memory_order_relaxed),
      render_last_completion_ns.exchange(0, std::memory_order_relaxed),
      render_inflight_capacity.exchange(0, std::memory_order_relaxed),
      render_inflight_high_water.exchange(0, std::memory_order_relaxed),
      render_inflight_pool_misses.exchange(0, std::memory_order_relaxed),
      render_output_capacity.exchange(0, std::memory_order_relaxed),
      render_output_high_water.exchange(0, std::memory_order_relaxed),
      render_output_pool_misses.exchange(0, std::memory_order_relaxed),
      frame_age_p99,
      preview_drops.exchange(0, std::memory_order_relaxed),
      encode_drops.exchange(0, std::memory_order_relaxed),
      decoded_pixel_host_copies.exchange(0, std::memory_order_relaxed),
      pool_exhaustion.exchange(0, std::memory_order_relaxed),
      native_texture_wrappers.exchange(0, std::memory_order_relaxed),
      native_command_buffers.exchange(0, std::memory_order_relaxed),
      native_decode_tickets.exchange(0, std::memory_order_relaxed),
      native_callback_wrappers.exchange(0, std::memory_order_relaxed),
  };
}

}  // namespace swim::core
