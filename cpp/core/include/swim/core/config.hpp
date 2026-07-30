#pragma once

#include <swim/core/camera_capacity.hpp>

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
  // How far into this clip the common time axis starts. Recorded files do not
  // share a t=0 — each stream begins at its own decodable keyframe, placed
  // somewhere inside the lookback window with GOP granularity — so the caller
  // derives this from the sample manifest's wall clocks. Zero means "read from
  // the file's first frame", which is right for live sources.
  std::chrono::milliseconds start_offset{0};
};

struct BenchmarkManifest final {
  std::string run_id;
  std::string asset_sha256;
  std::array<std::string, kMaxCameras> source_sha256;
};

// Camera identity is data, not code: `source.<id>=<path>` lines define both the
// lane order and the ids, so a 16-plane underwater layout and the 6-camera pool
// layout share one runtime. The default remains the pool order so existing
// configs and CLI-only invocations behave exactly as before.
inline constexpr std::array<std::string_view, 6> kPoolCameraIds{
    "cam3", "cam2", "cam1", "cam4", "cam5", "cam6"};

struct AppConfig {
  std::string backend{"metal"};
  RunMode mode{RunMode::realtime};
  BenchmarkStage stage{BenchmarkStage::full};
  std::filesystem::path asset_path;
  std::array<SourceConfig, kMaxCameras> sources{{
      {"cam3", {}},
      {"cam2", {}},
      {"cam1", {}},
      {"cam4", {}},
      {"cam5", {}},
      {"cam6", {}},
  }};
  // Lanes actually described by the config; sources beyond this are unset.
  std::uint32_t source_count{6};
  std::uint32_t fps_num{30000};
  std::uint32_t fps_den{1001};
  bool preview{true};
  bool preview_visible{true};
  bool encode{false};
  bool diagnostic_replacement{false};
  // Restart each lane's clip when it runs out instead of letting the lane fail
  // (which substitutes a black replacement frame). Without it any
  // --duration-seconds past the shortest clip ends in "MP4 reached EOF before
  // global render deadline". Live sources never hit EOF, so this only affects
  // file playback.
  //
  // Lanes restart on a common content period, not at their own EOF: recorded
  // clips differ in usable length by tens of milliseconds, so looping at each
  // file's end would let them drift apart by that much on every pass. Zero
  // period means "use each file's natural end", which only stays in sync for
  // equal-length clips.
  bool loop_sources{false};
  std::chrono::milliseconds loop_period{0};
  // End the run cleanly when a clip runs out, instead of reporting the lane as
  // failed. Only meaningful with loop_sources off.
  bool stop_at_eof{false};
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
