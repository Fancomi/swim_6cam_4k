#include <swim/core/benchmark_stage.hpp>

#include <stdexcept>

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

}  // namespace swim::core
