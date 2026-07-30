#include "test_support.hpp"

#include <swim/core/config.hpp>
#include <swim/core/build_info.hpp>
#include <swim/core/benchmark_stage.hpp>
#include <swim/core/runtime_validation.hpp>

#include <array>
#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <string>
#include <string_view>
#include <utility>

namespace {

// RAII guard that restores (or unsets) a process environment variable so
// tests cannot leak state into one another.
class EnvVarGuard {
 public:
  explicit EnvVarGuard(std::string name) : name_(std::move(name)) {
    const auto* const previous = std::getenv(name_.c_str());
    if (previous != nullptr) {
      had_previous_ = true;
      previous_value_ = previous;
    }
  }

  EnvVarGuard(std::string name, std::string_view value)
      : EnvVarGuard(std::move(name)) {
    set(value);
  }

  EnvVarGuard(const EnvVarGuard&) = delete;
  EnvVarGuard& operator=(const EnvVarGuard&) = delete;

  void set(std::string_view value) {
    setenv(name_.c_str(), std::string(value).c_str(), 1);
  }

  void unset() { unsetenv(name_.c_str()); }

  ~EnvVarGuard() {
    if (had_previous_) {
      setenv(name_.c_str(), previous_value_.c_str(), 1);
    } else {
      unsetenv(name_.c_str());
    }
  }

 private:
  std::string name_;
  bool had_previous_{false};
  std::string previous_value_;
};

class TempConfigFile {
 public:
  explicit TempConfigFile(std::string_view contents) {
    path_ = std::filesystem::temp_directory_path() /
            "swim_config_env_expansion_test.conf";
    std::ofstream output(path_, std::ios::trunc);
    output << contents;
  }

  TempConfigFile(const TempConfigFile&) = delete;
  TempConfigFile& operator=(const TempConfigFile&) = delete;

  ~TempConfigFile() { std::filesystem::remove(path_); }

  const std::filesystem::path& path() const { return path_; }

 private:
  std::filesystem::path path_;
};

std::string valid_config_with_cam3(std::string_view cam3_path) {
  std::string config{
      "backend=metal\n"
      "mode=realtime\n"
      "stage=full\n"
      "asset=assets/test.swasset\n"
      "source.cam3="};
  config.append(cam3_path);
  config.append(
      "\n"
      "source.cam2=inputs/cam2.mp4\n"
      "source.cam1=inputs/cam1.mp4\n"
      "source.cam4=inputs/cam4.mp4\n"
      "source.cam5=inputs/cam5.mp4\n"
      "source.cam6=inputs/cam6.mp4\n"
      "fps_num=30000\n"
      "fps_den=1001\n"
      "preview=true\n"
      "encode=false\n"
      "diagnostic_replacement=false\n"
      "encode_path=outputs/test-output.h265\n"
      "stale_ms=100\n"
      "replace_ms=1000\n"
      "decode_surface_pool=8\n"
      "decode_ticket_pool=16\n"
      "render_inflight=3\n"
      "output_pool=4\n"
      "duration_seconds=10\n"
      "metrics=outputs/test.jsonl\n");
  return config;
}

using namespace std::chrono_literals;
using namespace std::string_view_literals;
using swim::core::AppConfig;
using swim::core::BenchmarkStage;
using swim::core::EncodeSink;
using swim::core::RunMode;

swim::core::RuntimeAsset compatible_runtime_asset() {
  swim::core::RuntimeAsset asset{
      .logical_width = 5001,
      .logical_height = 2101,
      .encoded_width = 5002,
      .encoded_height = 2102,
      .source_sha256 = {},
      .cameras = {},
  };
  constexpr std::array<std::string_view, 6> camera_ids{
      "cam3", "cam2", "cam1", "cam4", "cam5", "cam6"};
  for (const auto camera_id : camera_ids) {
    asset.cameras.push_back({.camera_id = std::string(camera_id)});
  }
  return asset;
}

std::filesystem::path fixture(std::string_view name) {
  return std::filesystem::path{SWIM_TEST_FIXTURE_DIR} / name;
}

std::string fixture_error(std::string_view name, std::size_t line,
                          std::string_view message) {
  return fixture(name).string() + ":" + std::to_string(line) + ": " +
         std::string(message);
}

}  // namespace

TEST_CASE(generated_build_identity_is_complete) {
  CHECK(!swim::core::build_info::git_sha.empty());
  CHECK(!swim::core::build_info::build_type.empty());
  CHECK(!swim::core::build_info::compiler.empty());
}

TEST_CASE(accepts_exact_runtime_asset_compatibility) {
  swim::core::validate_runtime_compatibility(AppConfig{},
                                              compatible_runtime_asset(),
                                              false);
}

TEST_CASE(rejects_zero_runtime_asset_logical_width) {
  auto asset = compatible_runtime_asset();
  asset.logical_width = 0;
  CHECK_THROWS_WITH(swim::core::validate_runtime_compatibility(AppConfig{},
                                                               asset, false),
                    "runtime asset logical dimensions must be nonzero");
}

TEST_CASE(rejects_wrong_runtime_asset_logical_height) {
  auto asset = compatible_runtime_asset();
  asset.logical_height = 0;
  CHECK_THROWS_WITH(swim::core::validate_runtime_compatibility(AppConfig{},
                                                               asset, false),
                    "runtime asset logical dimensions must be nonzero");
}

TEST_CASE(rejects_wrong_runtime_asset_encoded_width) {
  auto asset = compatible_runtime_asset();
  asset.encoded_width = 5001;
  CHECK_THROWS_WITH(
      swim::core::validate_runtime_compatibility(AppConfig{}, asset, false),
      "runtime asset encoded dimensions must be the logical size rounded up "
      "to even: expected 5002x2102, found 5001x2102");
}

TEST_CASE(rejects_wrong_runtime_asset_encoded_height) {
  auto asset = compatible_runtime_asset();
  asset.encoded_height = 2101;
  CHECK_THROWS_WITH(
      swim::core::validate_runtime_compatibility(AppConfig{}, asset, false),
      "runtime asset encoded dimensions must be the logical size rounded up "
      "to even: expected 5002x2102, found 5002x2101");
}

TEST_CASE(accepts_sixteen_camera_underwater_asset_dimensions) {
  // The 16-plane underwater panorama compiles to a different size than the
  // 6-camera pool; the runtime must accept whatever the asset declares.
  swim::core::RuntimeAsset asset{
      .logical_width = 6001,
      .logical_height = 721,
      .encoded_width = 6002,
      .encoded_height = 722,
      .source_sha256 = {},
      .cameras = {},
  };
  AppConfig config;
  config.source_count = 16;
  for (std::size_t index = 0; index < 16; ++index) {
    const auto camera_id = "underA" + std::to_string(index + 1);
    config.sources[index].camera_id = camera_id;
    asset.cameras.push_back({.camera_id = camera_id});
  }
  swim::core::validate_runtime_compatibility(config, asset, false);
}

TEST_CASE(rejects_camera_count_disagreement_between_config_and_asset) {
  auto asset = compatible_runtime_asset();
  asset.cameras.pop_back();
  CHECK_THROWS_WITH(
      swim::core::validate_runtime_compatibility(AppConfig{}, asset, false),
      "config declares 6 sources but the asset contains 5 cameras");
}

TEST_CASE(rejects_camera_id_mismatch_at_a_lane) {
  auto asset = compatible_runtime_asset();
  asset.cameras[2].camera_id = "cam9";
  CHECK_THROWS_WITH(
      swim::core::validate_runtime_compatibility(AppConfig{}, asset, false),
      "camera order mismatch at lane 2: config has 'cam1' but the asset has "
      "'cam9'");
}

TEST_CASE(loads_exact_camera_order_and_all_config_values) {
  const auto config = swim::core::load_config(fixture("valid.conf"));

  CHECK_EQ(config.backend, "metal");
  CHECK_EQ(config.mode, RunMode::realtime);
  CHECK_EQ(config.stage, BenchmarkStage::full);
  CHECK_EQ(config.asset_path, std::filesystem::path{"assets/test.swasset"});
  constexpr std::array<std::string_view, 6> camera_ids{
      "cam3", "cam2", "cam1", "cam4", "cam5", "cam6"};
  for (std::size_t index = 0; index < camera_ids.size(); ++index) {
    CHECK_EQ(config.sources[index].camera_id, camera_ids[index]);
    CHECK_EQ(config.sources[index].path,
             std::filesystem::path{"inputs"} /
                 (std::string(camera_ids[index]) + ".mp4"));
  }
  CHECK_EQ(config.fps_num, 30000u);
  CHECK_EQ(config.fps_den, 1001u);
  CHECK(config.preview);
  CHECK(!config.encode);
  CHECK(!config.diagnostic_replacement);
  CHECK_EQ(config.encode_path,
           std::filesystem::path{"outputs/test-output.h265"});
  CHECK_EQ(config.stale_after, 100ms);
  CHECK_EQ(config.replace_after, 1000ms);
  CHECK_EQ(config.decode_surface_pool, 8u);
  CHECK_EQ(config.decode_ticket_pool, 16u);
  CHECK_EQ(config.render_inflight, 3u);
  CHECK_EQ(config.output_pool, 4u);
  CHECK_EQ(config.duration, 10s);
  CHECK_EQ(config.metrics_path, std::filesystem::path{"outputs/test.jsonl"});
  CHECK(!config.validate_only);
  CHECK_EQ(config.stream_count, 6u);
  CHECK_EQ(config.encode_sink, EncodeSink::file);
}

TEST_CASE(expands_environment_variable_in_path_valued_config_keys) {
  const auto temp_dir = std::filesystem::temp_directory_path() /
                        "swim_config_env_expansion_dataset";
  EnvVarGuard dataset_guard("SWIMMING_DATASET_DIR", temp_dir.string());

  TempConfigFile config_file(
      valid_config_with_cam3("${SWIMMING_DATASET_DIR}/cam3.mp4"));

  const auto config = swim::core::load_config(config_file.path());
  CHECK_EQ(config.sources[0].path, temp_dir / "cam3.mp4");
}

TEST_CASE(rejects_config_path_referencing_missing_environment_variable) {
  EnvVarGuard missing_guard("MISSING_DATASET_DIR");
  missing_guard.unset();

  TempConfigFile config_file(
      valid_config_with_cam3("${MISSING_DATASET_DIR}/cam3.mp4"));

  CHECK_THROWS_WITH(
      swim::core::load_config(config_file.path()),
      config_file.path().string() +
          ":5: environment variable 'MISSING_DATASET_DIR' is not set");
}

TEST_CASE(rejects_duplicate_config_key_at_the_second_occurrence) {
  CHECK_THROWS_WITH(
      swim::core::load_config(fixture("duplicate.conf")),
      fixture_error("duplicate.conf", 8, "duplicate key 'source.cam3'"));
}

TEST_CASE(rejects_unknown_config_key_with_source_location) {
  CHECK_THROWS_WITH(swim::core::load_config(fixture("unknown.conf")),
                    fixture_error("unknown.conf", 2,
                                  "unknown key 'surprise'"));
}

TEST_CASE(loads_declared_sources_in_file_order_without_a_fixed_table) {
  // missing.conf lists five lanes; camera identity and count are data now, so
  // the loader takes them verbatim rather than demanding the pool's six.
  const auto config = swim::core::load_config(fixture("missing.conf"));
  CHECK_EQ(config.source_count, 5u);
  CHECK_EQ(config.stream_count, 5u);
  constexpr std::array<std::string_view, 5> expected{"cam3", "cam2", "cam1",
                                                     "cam4", "cam5"};
  for (std::size_t index = 0; index < expected.size(); ++index) {
    CHECK_EQ(config.sources[index].camera_id, expected[index]);
  }
  CHECK(config.sources[5].camera_id.empty());
}

TEST_CASE(rejects_config_without_any_source_key) {
  CHECK_THROWS_WITH(
      swim::core::load_config(fixture("no-sources.conf")),
      fixture_error("no-sources.conf", 3,
                    "config must declare at least one "
                    "'source.<camera-id>' key"));
}

TEST_CASE(loads_per_lane_start_offsets_in_either_key_order) {
  // Recorded clips do not share a t=0, so each lane carries how far into its
  // file the common time axis begins. The key may precede or follow its
  // `source.<id>` line.
  const auto config =
      swim::core::load_config(fixture("underwater_start_ms.conf"));
  CHECK_EQ(config.source_count, 16u);
  CHECK_EQ(config.sources[0].camera_id, "underA16");
  CHECK_EQ(config.sources[0].start_offset, 100ms);
  CHECK_EQ(config.sources[15].camera_id, "underA1");
  CHECK_EQ(config.sources[15].start_offset, 1600ms);
  // lanes without the key stay at the live-source default of zero
  CHECK_EQ(config.sources[8].start_offset, 0ms);
}

TEST_CASE(rejects_start_ms_for_an_undeclared_camera) {
  CHECK_THROWS_WITH(
      swim::core::load_config(fixture("start-ms-unknown.conf")),
      fixture_error("start-ms-unknown.conf", 4,
                    "'source.underA9.start_ms' names a camera with no "
                    "'source.underA9' key"));
}

TEST_CASE(loads_sixteen_underwater_sources_in_declared_order) {
  const auto config = swim::core::load_config(fixture("underwater.conf"));
  CHECK_EQ(config.source_count, 16u);
  CHECK_EQ(config.stream_count, 16u);
  CHECK_EQ(config.sources[0].camera_id, "underA16");
  CHECK_EQ(config.sources[15].camera_id, "underA1");
}

TEST_CASE(applies_every_supported_cli_override) {
  AppConfig config;
  const std::array<std::string_view, 11> arguments{
      "--validate-only",
      "--preview=false",
      "--preview-visible=false",
      "--encode=true",
      "--diagnostic-replacement=true",
      "--encode-path=outputs/override.h265",
      "--encode-sink=null",
      "--duration-seconds=27",
      "--mode=benchmark",
      "--stage=decode-render-encode",
      "--stream-count=4",
  };
  config = swim::core::apply_cli_overrides(std::move(config), arguments);
  const std::array<std::string_view, 1> metrics{"--metrics=bench/run.jsonl"};
  config = swim::core::apply_cli_overrides(std::move(config), metrics);

  CHECK(config.validate_only);
  CHECK(!config.preview);
  CHECK(!config.preview_visible);
  CHECK(config.encode);
  CHECK(config.diagnostic_replacement);
  CHECK_EQ(config.encode_path,
           std::filesystem::path{"outputs/override.h265"});
  CHECK_EQ(config.encode_sink, EncodeSink::null_sink);
  CHECK_EQ(config.duration, 27s);
  CHECK_EQ(config.mode, RunMode::benchmark);
  CHECK_EQ(config.stage, BenchmarkStage::decode_render_encode);
  CHECK_EQ(config.stream_count, 4u);
  CHECK_EQ(config.metrics_path, std::filesystem::path{"bench/run.jsonl"});
}

TEST_CASE(loads_strict_benchmark_fingerprint_manifest) {
  auto config = AppConfig{};
  const std::array arguments{
      "--benchmark-manifest=tests/fixtures/cpp/benchmark.manifest"sv};
  config = swim::core::apply_cli_overrides(std::move(config), arguments);
  CHECK_EQ(config.benchmark_manifest_path,
           std::filesystem::path{"tests/fixtures/cpp/benchmark.manifest"});

  const auto manifest = swim::core::load_benchmark_manifest(
      fixture("benchmark.manifest"));
  CHECK_EQ(manifest.run_id, "run-20260711");
  CHECK_EQ(manifest.asset_sha256,
           "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
  CHECK_EQ(manifest.source_sha256[0],
           "0000000000000000000000000000000000000000000000000000000000000000");
  CHECK_EQ(manifest.source_sha256[5],
           "5555555555555555555555555555555555555555555555555555555555555555");
}

TEST_CASE(strict_benchmark_manifest_rejects_missing_duplicate_and_bad_hashes) {
  auto benchmark = AppConfig{};
  benchmark.mode = RunMode::benchmark;
  CHECK_THROWS_WITH(swim::core::resolve_benchmark_manifest(benchmark),
                    "benchmark mode requires --benchmark-manifest=PATH");

  benchmark.benchmark_manifest_path = fixture("benchmark-duplicate.manifest");
  CHECK_THROWS_WITH(
      swim::core::resolve_benchmark_manifest(benchmark),
      fixture_error("benchmark-duplicate.manifest", 3,
                    "duplicate key 'asset_sha256'"));

  benchmark.benchmark_manifest_path = fixture("benchmark-invalid.manifest");
  CHECK_THROWS_WITH(
      swim::core::resolve_benchmark_manifest(benchmark),
      fixture_error("benchmark-invalid.manifest", 2,
                    "asset_sha256 must be exactly 64 hexadecimal digits"));

  auto realtime = AppConfig{};
  CHECK(!swim::core::resolve_benchmark_manifest(realtime).has_value());
}

TEST_CASE(validates_hevc_encode_sink_path_and_fixed_frame_rate) {
  auto config = AppConfig{};
  config.encode = true;
  config.preview = false;
  config.encode_sink = EncodeSink::file;
  config.encode_path.clear();
  CHECK_THROWS_WITH(
      swim::core::validate_runtime_compatibility(config,
                                                 compatible_runtime_asset(),
                                                 true),
      "file HEVC encoding requires --encode-path");

  config.encode_path = "outputs/video.h264";
  CHECK_THROWS_WITH(
      swim::core::validate_runtime_compatibility(config,
                                                 compatible_runtime_asset(),
                                                 true),
      "HEVC encode path extension must be .h265 or .hevc");

  config.encode_path = "outputs/video.h265";
  swim::core::validate_runtime_compatibility(config,
                                              compatible_runtime_asset(), true);
  config.encode_path = "outputs/video.hevc";
  swim::core::validate_runtime_compatibility(config,
                                              compatible_runtime_asset(), true);

  config.fps_num = 30;
  CHECK_THROWS_WITH(
      swim::core::validate_runtime_compatibility(config,
                                                 compatible_runtime_asset(),
                                                 true),
      "HEVC encoding requires fps_num/fps_den=30000/1001");

  config.encode_sink = EncodeSink::null_sink;
  config.encode_path.clear();
  config.fps_num = 30000;
  swim::core::validate_runtime_compatibility(config,
                                              compatible_runtime_asset(), true);
}

TEST_CASE(runtime_validation_rejects_invalid_forced_encode_settings) {
  auto config = AppConfig{};
  config.stage = BenchmarkStage::decode_render_encode;
  config.encode = false;
  config.encode_sink = EncodeSink::null_sink;
  config.fps_num = 25;
  config.fps_den = 1;
  const auto graph = swim::core::resolve_benchmark_graph(config);

  CHECK(graph.encode);
  CHECK_THROWS_WITH(
      swim::core::validate_runtime_compatibility(
          config, compatible_runtime_asset(), graph.encode),
      "HEVC encoding requires fps_num/fps_den=30000/1001");
  CHECK(!config.encode);

  config.fps_num = 30000;
  config.fps_den = 1001;
  config.encode_sink = EncodeSink::file;
  config.encode_path = "outputs/video.h264";
  CHECK_THROWS_WITH(
      swim::core::validate_runtime_compatibility(
          config, compatible_runtime_asset(), graph.encode),
      "HEVC encode path extension must be .h265 or .hevc");
  CHECK(!config.encode);
}

TEST_CASE(runtime_validation_ignores_raw_encode_when_graph_forces_it_off) {
  auto config = AppConfig{};
  config.stage = BenchmarkStage::render_only;
  config.encode = true;
  config.encode_sink = EncodeSink::file;
  config.encode_path = "outputs/video.h264";
  config.fps_num = 25;
  config.fps_den = 1;
  const auto graph = swim::core::resolve_benchmark_graph(config);

  CHECK(!graph.encode);
  swim::core::validate_runtime_compatibility(
      config, compatible_runtime_asset(), graph.encode);
  CHECK(config.encode);
  CHECK_EQ(config.fps_num, 25u);
  CHECK_EQ(config.encode_path, std::filesystem::path{"outputs/video.h264"});
}

TEST_CASE(accepts_every_exact_stage_and_stream_count_value) {
  struct StageCase {
    std::string_view argument;
    BenchmarkStage expected;
  };
  constexpr std::array<StageCase, 6> stages{{
      {"--stage=full", BenchmarkStage::full},
      {"--stage=decode-only", BenchmarkStage::decode_only},
      {"--stage=render-only", BenchmarkStage::render_only},
      {"--stage=decode-render", BenchmarkStage::decode_render},
      {"--stage=decode-render-preview",
       BenchmarkStage::decode_render_preview},
      {"--stage=decode-render-encode", BenchmarkStage::decode_render_encode},
  }};
  for (const auto& stage : stages) {
    const std::array arguments{stage.argument};
    const auto config =
        swim::core::apply_cli_overrides(AppConfig{}, arguments);
    CHECK_EQ(config.stage, stage.expected);
  }

  constexpr std::array<std::uint32_t, 4> counts{1, 2, 4, 6};
  constexpr std::array<std::string_view, 4> arguments{
      "--stream-count=1", "--stream-count=2", "--stream-count=4",
      "--stream-count=6"};
  for (std::size_t index = 0; index < counts.size(); ++index) {
    const std::array one_argument{arguments[index]};
    const auto config =
        swim::core::apply_cli_overrides(AppConfig{}, one_argument);
    CHECK_EQ(config.stream_count, counts[index]);
  }
}

TEST_CASE(rejects_repeated_unknown_and_non_exact_cli_values) {
  const std::array repeated{"--preview=true"sv, "--preview=false"sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, repeated),
                    "duplicate command-line option '--preview'");

  const std::array unknown{"--wat=true"sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, unknown),
                    "unknown command-line option '--wat=true'");

  const std::array bad_boolean{"--encode=1"sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, bad_boolean),
                    "--encode must be true or false");

  const std::array bad_stage{"--stage=decode_render"sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, bad_stage),
                    "invalid --stage value 'decode_render'");

  const std::array bad_count{"--stream-count=17"sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, bad_count),
                    "--stream-count must be between 1 and 16");

  const std::array missing_encode_path{"--encode-path"sv};
  CHECK_THROWS_WITH(
      swim::core::apply_cli_overrides(AppConfig{}, missing_encode_path),
      "--encode-path requires PATH");

  const std::array missing_metrics_path{"--metrics="sv};
  CHECK_THROWS_WITH(
      swim::core::apply_cli_overrides(AppConfig{}, missing_metrics_path),
      "--metrics requires PATH");
}

TEST_CASE(rejects_bare_duration_before_unsigned_parsing) {
  const std::array arguments{"--duration-seconds"sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, arguments),
                    "--duration-seconds must be an unsigned integer");
}

TEST_CASE(rejects_bare_stream_count_before_unsigned_parsing) {
  const std::array arguments{"--stream-count"sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, arguments),
                    "--stream-count must be an unsigned integer");
}

TEST_CASE(rejects_empty_duration_assignment) {
  const std::array arguments{"--duration-seconds="sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, arguments),
                    "--duration-seconds must be an unsigned integer");
}

TEST_CASE(rejects_empty_stream_count_assignment) {
  const std::array arguments{"--stream-count="sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, arguments),
                    "--stream-count must be an unsigned integer");
}

TEST_CASE(rejects_duration_uint32_overflow) {
  const std::array arguments{"--duration-seconds=4294967296"sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, arguments),
                    "--duration-seconds must be an unsigned integer");
}

TEST_CASE(rejects_stream_count_uint32_overflow) {
  const std::array arguments{"--stream-count=4294967296"sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, arguments),
                    "--stream-count must be an unsigned integer");
}

TEST_CASE(loads_loop_controls_with_a_shared_period) {
  const auto config = swim::core::load_config(fixture("underwater_loop.conf"));
  CHECK(config.loop_sources);
  CHECK_EQ(config.loop_period, 11961ms);
}

TEST_CASE(loop_controls_default_to_off) {
  const auto config = swim::core::load_config(fixture("valid.conf"));
  CHECK(!config.loop_sources);
  CHECK_EQ(config.loop_period, 0ms);
}

TEST_CASE(applies_loop_cli_overrides) {
  const std::array arguments{"--loop-sources=true"sv, "--loop-period-ms=5000"sv};
  const auto config =
      swim::core::apply_cli_overrides(AppConfig{}, arguments);
  CHECK(config.loop_sources);
  CHECK_EQ(config.loop_period, 5000ms);
}
