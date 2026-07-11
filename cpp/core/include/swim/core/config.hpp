#pragma once

#include <array>
#include <chrono>
#include <cstdint>
#include <filesystem>
#include <span>
#include <optional>
#include <string>
#include <string_view>

namespace swim::core {

enum class RunMode : std::uint8_t {
  realtime,
  benchmark,
};

enum class BenchmarkStage : std::uint8_t {
  full,
  decode_only,
  render_only,
  decode_render,
  decode_render_preview,
  decode_render_encode,
};

enum class EncodeSink : std::uint8_t {
  file,
  null_sink,
};

struct SourceConfig {
  std::string camera_id;
  std::filesystem::path path;
};

struct BenchmarkManifest final {
  std::string run_id;
  std::string asset_sha256;
  std::array<std::string, 6> source_sha256;
};

struct AppConfig {
  std::string backend{"metal"};
  RunMode mode{RunMode::realtime};
  BenchmarkStage stage{BenchmarkStage::full};
  std::filesystem::path asset_path;
  std::array<SourceConfig, 6> sources{{
      {"cam3", {}},
      {"cam2", {}},
      {"cam1", {}},
      {"cam4", {}},
      {"cam5", {}},
      {"cam6", {}},
  }};
  std::uint32_t fps_num{30000};
  std::uint32_t fps_den{1001};
  bool preview{true};
  bool encode{false};
  bool diagnostic_replacement{false};
  std::filesystem::path encode_path;
  std::chrono::milliseconds stale_after{100};
  std::chrono::milliseconds replace_after{1000};
  std::uint32_t decode_surface_pool{8};
  std::uint32_t decode_ticket_pool{16};
  std::uint32_t render_inflight{3};
  std::uint32_t output_pool{4};
  std::chrono::seconds duration{10};
  std::filesystem::path metrics_path;
  std::filesystem::path benchmark_manifest_path;
  bool validate_only{false};
  std::uint32_t stream_count{6};
  EncodeSink encode_sink{EncodeSink::file};
};

AppConfig load_config(const std::filesystem::path& path);
AppConfig apply_cli_overrides(AppConfig config,
                              std::span<const std::string_view> arguments);
BenchmarkManifest load_benchmark_manifest(const std::filesystem::path& path);
std::optional<BenchmarkManifest> resolve_benchmark_manifest(
    const AppConfig& config);

}  // namespace swim::core
