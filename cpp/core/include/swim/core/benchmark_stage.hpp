#pragma once

#include <swim/core/config.hpp>

#include <cstdint>
#include <string_view>

namespace swim::core {

struct BenchmarkGraph final {
  std::uint32_t active_sources{};
  bool create_renderer{};
  bool synthetic_inputs{};
  bool preview{};
  bool encode{};
};

BenchmarkGraph resolve_benchmark_graph(const AppConfig& config);
std::string_view benchmark_stage_name(BenchmarkStage stage) noexcept;
std::string_view pacing_name(RunMode mode) noexcept;

}  // namespace swim::core
