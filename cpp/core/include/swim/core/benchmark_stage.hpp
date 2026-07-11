#pragma once

#include <swim/core/config.hpp>
#include <swim/core/backend.hpp>
#include <swim/core/run_lifecycle.hpp>

#include <chrono>
#include <cstdint>
#include <stop_token>
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

SourceArray make_sources(IBackend& backend, const AppConfig& config,
                         std::uint32_t active_sources);
void stop_sources(SourceArray& sources) noexcept;

enum class DecodeOnlyExit : std::uint8_t {
  deadline_reached,
  stop_requested,
  all_sources_failed_before_active,
};

DecodeOnlyExit run_decode_only(
    SourceArray& sources, MailboxArray& mailboxes,
    std::uint32_t active_sources, RunLifecycle& lifecycle,
    std::stop_token token,
    std::chrono::milliseconds backoff = std::chrono::milliseconds{1});

}  // namespace swim::core
