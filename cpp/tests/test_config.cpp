#include "test_support.hpp"

#include <swim/core/config.hpp>

#include <array>
#include <chrono>
#include <filesystem>
#include <string>
#include <string_view>

namespace {

using namespace std::chrono_literals;
using namespace std::string_view_literals;
using swim::core::AppConfig;
using swim::core::BenchmarkStage;
using swim::core::EncodeSink;
using swim::core::RunMode;

std::filesystem::path fixture(std::string_view name) {
  return std::filesystem::path{SWIM_TEST_FIXTURE_DIR} / name;
}

std::string fixture_error(std::string_view name, std::size_t line,
                          std::string_view message) {
  return fixture(name).string() + ":" + std::to_string(line) + ": " +
         std::string(message);
}

}  // namespace

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
           std::filesystem::path{"outputs/test-output.h264"});
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

TEST_CASE(rejects_missing_camera_instead_of_reordering_sources) {
  CHECK_THROWS_WITH(
      swim::core::load_config(fixture("missing.conf")),
      fixture_error("missing.conf", 8,
                    "sources must be exactly cam3,cam2,cam1,cam4,cam5,cam6"));
}

TEST_CASE(applies_every_supported_cli_override) {
  AppConfig config;
  const std::array<std::string_view, 10> arguments{
      "--validate-only",
      "--preview=false",
      "--encode=true",
      "--diagnostic-replacement=true",
      "--encode-path=outputs/override.h264",
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
  CHECK(config.encode);
  CHECK(config.diagnostic_replacement);
  CHECK_EQ(config.encode_path,
           std::filesystem::path{"outputs/override.h264"});
  CHECK_EQ(config.encode_sink, EncodeSink::null_sink);
  CHECK_EQ(config.duration, 27s);
  CHECK_EQ(config.mode, RunMode::benchmark);
  CHECK_EQ(config.stage, BenchmarkStage::decode_render_encode);
  CHECK_EQ(config.stream_count, 4u);
  CHECK_EQ(config.metrics_path, std::filesystem::path{"bench/run.jsonl"});
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

  const std::array bad_count{"--stream-count=3"sv};
  CHECK_THROWS_WITH(swim::core::apply_cli_overrides(AppConfig{}, bad_count),
                    "--stream-count must be one of 1,2,4,6");

  const std::array missing_encode_path{"--encode-path"sv};
  CHECK_THROWS_WITH(
      swim::core::apply_cli_overrides(AppConfig{}, missing_encode_path),
      "--encode-path requires PATH");

  const std::array missing_metrics_path{"--metrics="sv};
  CHECK_THROWS_WITH(
      swim::core::apply_cli_overrides(AppConfig{}, missing_metrics_path),
      "--metrics requires PATH");
}
