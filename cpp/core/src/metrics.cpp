#include <swim/core/metrics.hpp>

#include <swim/core/hot_path_allocations.hpp>

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace swim::core {

namespace {

template <std::size_t Size>
std::array<std::uint64_t, Size> sample_atomic_array(
    const std::array<std::atomic_uint64_t, Size>& source) noexcept {
  std::array<std::uint64_t, Size> result{};
  for (std::size_t index = 0; index < Size; ++index) {
    result[index] = source[index].load(std::memory_order_relaxed);
  }
  return result;
}

template <std::size_t Size>
void reset_atomic_array(
    std::array<std::atomic_uint64_t, Size>& source) noexcept {
  for (auto& value : source) {
    value.store(0, std::memory_order_relaxed);
  }
}

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

std::uint64_t MetricsSnapshot::encode_completion_interval_ns() const noexcept {
  return encode_last_completion_ns > encode_first_submit_ns
             ? encode_last_completion_ns - encode_first_submit_ns
             : 0;
}

double MetricsSnapshot::encode_completion_fps() const noexcept {
  const auto interval = encode_completion_interval_ns();
  return interval == 0
             ? 0.0
             : static_cast<double>(encode_completions) * 1'000'000'000.0 /
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
  buckets_[static_cast<std::size_t>(bucket)].fetch_add(
      1, std::memory_order_relaxed);
}

std::chrono::milliseconds FixedLatencyHistogram::percentile(
    double quantile) const {
  return sample().percentile(quantile);
}

FixedLatencyHistogramSnapshot FixedLatencyHistogram::sample() const noexcept {
  std::array<std::uint64_t, kBucketCount> buckets{};
  std::uint64_t count = 0;
  for (std::size_t index = 0; index < buckets.size(); ++index) {
    buckets[index] = buckets_[index].load(std::memory_order_relaxed);
    count += buckets[index];
  }
  return FixedLatencyHistogramSnapshot{buckets, count};
}

FixedLatencyHistogramSnapshot FixedLatencyHistogram::snapshot_and_reset()
    noexcept {
  std::array<std::uint64_t, kBucketCount> buckets{};
  std::uint64_t count = 0;
  for (std::size_t index = 0; index < buckets.size(); ++index) {
    buckets[index] =
        buckets_[index].exchange(0, std::memory_order_relaxed);
    count += buckets[index];
  }
  return FixedLatencyHistogramSnapshot{buckets, count};
}

std::uint64_t FixedLatencyHistogram::count() const noexcept {
  return sample().count();
}

MetricsSnapshot RuntimeCounters::sample_totals() const noexcept {
  std::array<std::uint64_t, 6> frame_age_p50{};
  std::array<std::uint64_t, 6> frame_age_p95{};
  std::array<std::uint64_t, 6> frame_age_p99{};
  for (std::size_t camera = 0; camera < frame_age.size(); ++camera) {
    const auto snapshot = frame_age[camera].sample();
    frame_age_p50[camera] = static_cast<std::uint64_t>(
        snapshot.percentile(0.50).count());
    frame_age_p95[camera] = static_cast<std::uint64_t>(
        snapshot.percentile(0.95).count());
    frame_age_p99[camera] = static_cast<std::uint64_t>(
        snapshot.percentile(0.99).count());
  }
  const auto age_spread = snapshot_age_spread.sample();
  const auto gpu_duration = gpu_render_duration.sample();

  return MetricsSnapshot{
      received.load(std::memory_order_relaxed),
      decoded.load(std::memory_order_relaxed),
      published.load(std::memory_order_relaxed),
      overwritten.load(std::memory_order_relaxed),
      reused.load(std::memory_order_relaxed),
      malformed.load(std::memory_order_relaxed),
      reconnects.load(std::memory_order_relaxed),
      render_submissions.load(std::memory_order_relaxed),
      render_completions.load(std::memory_order_relaxed),
      render_drops.load(std::memory_order_relaxed),
      render_active_ns.load(std::memory_order_relaxed),
      render_first_submit_ns.load(std::memory_order_relaxed),
      render_last_completion_ns.load(std::memory_order_relaxed),
      render_inflight_capacity.load(std::memory_order_relaxed),
      render_inflight_in_use.load(std::memory_order_relaxed),
      render_inflight_high_water.load(std::memory_order_relaxed),
      render_inflight_pool_misses.load(std::memory_order_relaxed),
      render_output_capacity.load(std::memory_order_relaxed),
      render_output_in_use.load(std::memory_order_relaxed),
      render_output_high_water.load(std::memory_order_relaxed),
      render_output_pool_misses.load(std::memory_order_relaxed),
      sample_atomic_array(camera_received),
      sample_atomic_array(camera_decoded),
      sample_atomic_array(camera_published),
      sample_atomic_array(camera_overwritten),
      sample_atomic_array(camera_reused),
      frame_age_p50,
      frame_age_p95,
      frame_age_p99,
      static_cast<std::uint64_t>(
          age_spread.percentile(0.99).count()),
      static_cast<std::uint64_t>(
          gpu_duration.percentile(0.50).count()),
      static_cast<std::uint64_t>(
          gpu_duration.percentile(0.95).count()),
      preview_submissions.load(std::memory_order_relaxed),
      preview_completions.load(std::memory_order_relaxed),
      preview_drops.load(std::memory_order_relaxed),
      preview_presents.load(std::memory_order_relaxed),
      encode_submissions.load(std::memory_order_relaxed),
      encode_completions.load(std::memory_order_relaxed),
      encode_bytes.load(std::memory_order_relaxed),
      encode_drops.load(std::memory_order_relaxed),
      encode_rejected_frames.load(std::memory_order_relaxed),
      encode_callback_errors.load(std::memory_order_relaxed),
      encode_first_submit_ns.load(std::memory_order_relaxed),
      encode_last_completion_ns.load(std::memory_order_relaxed),
      encode_input_capacity.load(std::memory_order_relaxed),
      encode_input_in_use.load(std::memory_order_relaxed),
      encode_input_high_water.load(std::memory_order_relaxed),
      encode_input_pool_misses.load(std::memory_order_relaxed),
      encode_using_hardware.load(std::memory_order_relaxed) != 0,
      encode_drain_timeouts.load(std::memory_order_relaxed),
      decoded_pixel_host_copies.load(std::memory_order_relaxed),
      pool_exhaustion.load(std::memory_order_relaxed),
      native_texture_wrappers.load(std::memory_order_relaxed),
      native_command_buffers.load(std::memory_order_relaxed),
      native_decode_tickets.load(std::memory_order_relaxed),
      native_callback_wrappers.load(std::memory_order_relaxed),
      hot_path_allocation_count(),
      sample_atomic_array(decode_surface_capacity),
      sample_atomic_array(decode_surface_in_use),
      sample_atomic_array(decode_surface_high_water),
      sample_atomic_array(decode_surface_pool_misses),
      sample_atomic_array(decode_ticket_capacity),
      sample_atomic_array(decode_ticket_in_use),
      sample_atomic_array(decode_ticket_high_water),
      sample_atomic_array(decode_ticket_pool_misses),
  };
}

MetricsSnapshot RuntimeCounters::snapshot_and_reset() noexcept {
  auto snapshot = sample_totals();
  received.store(0, std::memory_order_relaxed);
  decoded.store(0, std::memory_order_relaxed);
  published.store(0, std::memory_order_relaxed);
  overwritten.store(0, std::memory_order_relaxed);
  reused.store(0, std::memory_order_relaxed);
  malformed.store(0, std::memory_order_relaxed);
  reconnects.store(0, std::memory_order_relaxed);
  render_submissions.store(0, std::memory_order_relaxed);
  render_completions.store(0, std::memory_order_relaxed);
  render_drops.store(0, std::memory_order_relaxed);
  render_active_ns.store(0, std::memory_order_relaxed);
  render_first_submit_ns.store(0, std::memory_order_relaxed);
  render_last_completion_ns.store(0, std::memory_order_relaxed);
  render_inflight_capacity.store(0, std::memory_order_relaxed);
  render_inflight_in_use.store(0, std::memory_order_relaxed);
  render_inflight_high_water.store(0, std::memory_order_relaxed);
  render_inflight_pool_misses.store(0, std::memory_order_relaxed);
  render_output_capacity.store(0, std::memory_order_relaxed);
  render_output_in_use.store(0, std::memory_order_relaxed);
  render_output_high_water.store(0, std::memory_order_relaxed);
  render_output_pool_misses.store(0, std::memory_order_relaxed);
  preview_submissions.store(0, std::memory_order_relaxed);
  preview_completions.store(0, std::memory_order_relaxed);
  preview_drops.store(0, std::memory_order_relaxed);
  preview_presents.store(0, std::memory_order_relaxed);
  encode_submissions.store(0, std::memory_order_relaxed);
  encode_completions.store(0, std::memory_order_relaxed);
  encode_bytes.store(0, std::memory_order_relaxed);
  encode_drops.store(0, std::memory_order_relaxed);
  encode_rejected_frames.store(0, std::memory_order_relaxed);
  encode_callback_errors.store(0, std::memory_order_relaxed);
  encode_first_submit_ns.store(0, std::memory_order_relaxed);
  encode_last_completion_ns.store(0, std::memory_order_relaxed);
  encode_input_capacity.store(0, std::memory_order_relaxed);
  encode_input_in_use.store(0, std::memory_order_relaxed);
  encode_input_high_water.store(0, std::memory_order_relaxed);
  encode_input_pool_misses.store(0, std::memory_order_relaxed);
  encode_using_hardware.store(0, std::memory_order_relaxed);
  encode_drain_timeouts.store(0, std::memory_order_relaxed);
  decoded_pixel_host_copies.store(0, std::memory_order_relaxed);
  pool_exhaustion.store(0, std::memory_order_relaxed);
  native_texture_wrappers.store(0, std::memory_order_relaxed);
  native_command_buffers.store(0, std::memory_order_relaxed);
  native_decode_tickets.store(0, std::memory_order_relaxed);
  native_callback_wrappers.store(0, std::memory_order_relaxed);
  reset_atomic_array(camera_received);
  reset_atomic_array(camera_decoded);
  reset_atomic_array(camera_published);
  reset_atomic_array(camera_overwritten);
  reset_atomic_array(camera_reused);
  reset_atomic_array(decode_surface_capacity);
  reset_atomic_array(decode_surface_in_use);
  reset_atomic_array(decode_surface_high_water);
  reset_atomic_array(decode_surface_pool_misses);
  reset_atomic_array(decode_ticket_capacity);
  reset_atomic_array(decode_ticket_in_use);
  reset_atomic_array(decode_ticket_high_water);
  reset_atomic_array(decode_ticket_pool_misses);
  for (auto& histogram : frame_age) {
    static_cast<void>(histogram.snapshot_and_reset());
  }
  static_cast<void>(snapshot_age_spread.snapshot_and_reset());
  static_cast<void>(gpu_render_duration.snapshot_and_reset());
  return snapshot;
}

std::uint64_t monotonic_delta(std::uint64_t current,
                              std::uint64_t previous) noexcept {
  return current >= previous ? current - previous : 0;
}

}  // namespace swim::core
