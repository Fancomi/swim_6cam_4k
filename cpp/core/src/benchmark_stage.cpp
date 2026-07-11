#include <swim/core/benchmark_stage.hpp>

#include <condition_variable>
#include <mutex>
#include <stdexcept>
#include <thread>

namespace swim::core {

BenchmarkGraph resolve_benchmark_graph(const AppConfig& config) {
  switch (config.stream_count) {
    case 1:
    case 2:
    case 4:
    case 6:
      break;
    default:
      throw std::invalid_argument(
          "stream_count must be one of 1, 2, 4, or 6");
  }

  switch (config.stage) {
    case BenchmarkStage::full:
      return {config.stream_count, true, false, config.preview, config.encode};
    case BenchmarkStage::decode_only:
      return {config.stream_count, false, false, false, false};
    case BenchmarkStage::render_only:
      return {0, true, true, false, false};
    case BenchmarkStage::decode_render:
      return {config.stream_count, true, false, false, false};
    case BenchmarkStage::decode_render_preview:
      return {config.stream_count, true, false, true, false};
    case BenchmarkStage::decode_render_encode:
      return {config.stream_count, true, false, false, true};
  }
  throw std::invalid_argument("unsupported benchmark stage");
}

std::string_view benchmark_stage_name(BenchmarkStage stage) noexcept {
  switch (stage) {
    case BenchmarkStage::full:
      return "full";
    case BenchmarkStage::decode_only:
      return "decode-only";
    case BenchmarkStage::render_only:
      return "render-only";
    case BenchmarkStage::decode_render:
      return "decode-render";
    case BenchmarkStage::decode_render_preview:
      return "decode-render-preview";
    case BenchmarkStage::decode_render_encode:
      return "decode-render-encode";
  }
  return "unknown";
}

std::string_view pacing_name(RunMode mode) noexcept {
  switch (mode) {
    case RunMode::realtime:
      return "realtime";
    case RunMode::benchmark:
      return "benchmark";
  }
  return "unknown";
}

SourceArray make_sources(IBackend& backend, const AppConfig& config,
                         std::uint32_t active_sources) {
  if (active_sources > config.sources.size()) {
    throw std::invalid_argument("active_sources must not exceed six");
  }
  SourceArray sources;
  for (std::uint32_t camera = 0; camera < active_sources; ++camera) {
    sources[camera] = backend.make_source(config.sources[camera], camera);
  }
  return sources;
}

void stop_sources(SourceArray& sources) noexcept {
  for (auto& source : sources) {
    if (source) {
      source->stop();
    }
  }
}

DecodeOnlyExit run_decode_only(SourceArray& sources, MailboxArray& mailboxes,
                               std::uint32_t active_sources,
                               RunLifecycle& lifecycle,
                               std::stop_token token,
                               std::chrono::milliseconds backoff) {
  if (active_sources == 0 || active_sources > sources.size()) {
    throw std::invalid_argument(
        "decode-only active_sources must be between one and six");
  }
  if (backoff <= std::chrono::milliseconds::zero()) {
    throw std::invalid_argument("decode-only backoff must be positive");
  }

  std::condition_variable_any condition;
  std::mutex mutex;
  while (true) {
    const auto now = RunLifecycle::Clock::now();
    for (std::uint32_t camera = 0; camera < active_sources; ++camera) {
      FrameLease decoded;
      if (mailboxes[camera].consume_latest(decoded)) {
        static_cast<void>(lifecycle.mark_active(now));
      }
    }

    if (lifecycle.deadline_reached(now)) {
      return DecodeOnlyExit::deadline_reached;
    }
    if (token.stop_requested() || lifecycle.stop_requested()) {
      return DecodeOnlyExit::stop_requested;
    }
    if (!lifecycle.active()) {
      bool all_failed = true;
      for (std::uint32_t camera = 0; camera < active_sources; ++camera) {
        if (sources[camera] && !sources[camera]->failed()) {
          all_failed = false;
          break;
        }
      }
      if (all_failed) {
        lifecycle.request_stop();
        return DecodeOnlyExit::all_sources_failed_before_active;
      }
    }

    std::unique_lock lock(mutex);
    condition.wait_for(lock, token, backoff, [] { return false; });
  }
}

}  // namespace swim::core
