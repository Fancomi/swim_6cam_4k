#include "test_support.hpp"

#include <swim/core/benchmark_reporter.hpp>
#include <swim/core/benchmark_stage.hpp>

#include <array>
#include <chrono>
#include <filesystem>
#include <string>

using namespace std::chrono_literals;

namespace {

swim::core::BenchmarkReporterMetadata reporter_metadata() {
  swim::core::AppConfig config;
  config.backend = "metal\"test";
  config.mode = swim::core::RunMode::benchmark;
  config.stage = swim::core::BenchmarkStage::decode_render_encode;
  config.stream_count = 6;
  config.preview = false;
  config.encode = true;
  config.metrics_path = "/tmp/swim_benchmark_reporter_test.jsonl";

  swim::core::BenchmarkManifest manifest;
  manifest.run_id = "run\\id\nline";
  manifest.asset_sha256 = std::string(64, 'a');
  for (std::size_t camera = 0; camera < manifest.source_sha256.size();
       ++camera) {
    manifest.source_sha256[camera] =
        std::string(64, static_cast<char>('0' + camera));
  }
  return {config, swim::core::resolve_benchmark_graph(config), manifest,
          5002, 2102, {}};
}

class SampleBackend final : public swim::core::IBackend {
 public:
  std::unique_ptr<swim::core::ISource> make_source(
      const swim::core::SourceConfig&, std::uint32_t) override {
    return {};
  }
  std::unique_ptr<swim::core::IRenderer> make_renderer(
      const swim::core::RuntimeAsset&, const swim::core::AppConfig&,
      const swim::core::BenchmarkGraph&) override {
    return {};
  }
  void run_main_loop(std::stop_token) override {}
  void stop_main_loop() noexcept override {}
  swim::core::BackendRuntimeSample sample_runtime() const noexcept override {
    return {123456};
  }
};

}  // namespace

TEST_CASE(benchmark_json_escapes_strings_and_contains_exact_schema_groups) {
  swim::core::RuntimeCounters counters;
  for (std::size_t camera = 0; camera < 6; ++camera) {
    counters.camera_received[camera].store(camera + 1);
    counters.camera_overwritten[camera].store(camera + 10);
    counters.camera_reused[camera].store(camera + 20);
    counters.frame_age[camera].observe(
        std::chrono::milliseconds{static_cast<std::int64_t>(camera + 30)});
  }
  counters.render_completions.store(30);
  counters.preview_completions.store(20);
  counters.encode_completions.store(10);
  const auto line = swim::core::serialize_benchmark_record(
      reporter_metadata(), counters.sample_totals(), {}, {123456}, 2.0,
      false, 6, 987654);

  CHECK(line.ends_with('\n'));
  CHECK(line.find("\"schema\":1") != std::string::npos);
  CHECK(line.find("\"run_id\":\"run\\\\id\\nline\"") !=
        std::string::npos);
  CHECK(line.find("\"backend\":\"metal\\\"test\"") !=
        std::string::npos);
  CHECK(line.find("\"pacing\":\"unpaced\"") != std::string::npos);
  CHECK(line.find("\"resolved_graph\":{") != std::string::npos);
  CHECK(line.find("\"build_type\":") != std::string::npos);
  CHECK(line.find("\"git_sha\":") != std::string::npos);
  CHECK(line.find("\"source_sha256\":[") != std::string::npos);
  CHECK(line.find("\"frame_age_ms_p50\":[30,31,32,33,34,35]") !=
        std::string::npos);
  CHECK(line.find("\"mailbox_overwrites\":[10,11,12,13,14,15]") !=
        std::string::npos);
  CHECK(line.find("\"gpu_allocated_bytes\":123456") !=
        std::string::npos);
  CHECK(line.find("\"rss_bytes\":987654") != std::string::npos);
}

TEST_CASE(reporter_uses_one_write_call_and_final_remains_cumulative) {
  std::filesystem::remove("/tmp/swim_benchmark_reporter_test.jsonl");
  swim::core::RuntimeCounters counters;
  SampleBackend backend;
  std::size_t writes = 0;
  std::string captured;
  auto writer = [&](int, const void* bytes, std::size_t size) -> std::ptrdiff_t {
    ++writes;
    captured.assign(static_cast<const char*>(bytes), size);
    return static_cast<std::ptrdiff_t>(size);
  };
  swim::core::BenchmarkReporter reporter{reporter_metadata(), counters,
                                          writer};
  reporter.bind_backend(backend);

  counters.encode_completions.store(4);
  counters.frame_age[0].observe(10ms);
  reporter.write_interval_now();
  CHECK_EQ(writes, 1u);
  CHECK_EQ(captured.back(), '\n');
  CHECK(captured.find("\"final\":false") != std::string::npos);

  counters.encode_completions.fetch_add(3);
  counters.frame_age[0].observe(100ms);
  reporter.write_interval_now();
  CHECK_EQ(writes, 2u);
  CHECK(captured.find("\"frame_age_ms_p50\":[100,0,0,0,0,0]") !=
        std::string::npos);
  reporter.write_final(6);
  CHECK_EQ(writes, 3u);
  CHECK(captured.find("\"final\":true") != std::string::npos);
  CHECK(captured.find("\"encode_completions\":7") !=
        std::string::npos);
  reporter.write_final(6);
  CHECK_EQ(writes, 3u);
}

TEST_CASE(schema_keeps_unverified_hash_keys_and_final_rates_use_completion_span) {
  swim::core::AppConfig config;
  config.mode = swim::core::RunMode::realtime;
  config.stage = swim::core::BenchmarkStage::decode_render_encode;
  swim::core::BenchmarkReporterMetadata metadata{
      config, swim::core::resolve_benchmark_graph(config), std::nullopt,
      5002, 2102, "generated-run"};
  swim::core::RuntimeCounters counters;
  counters.render_completions.store(90);
  counters.render_first_submit_ns.store(1'000'000'000);
  counters.render_last_completion_ns.store(4'000'000'000);
  counters.encode_completions.store(60);
  counters.encode_first_submit_ns.store(2'000'000'000);
  counters.encode_last_completion_ns.store(4'000'000'000);

  const auto line = swim::core::serialize_benchmark_record(
      metadata, counters.sample_totals(), {}, {}, 10.0, true, 6, 1);
  CHECK(line.find("\"render_fps\":30.000") != std::string::npos);
  CHECK(line.find("\"encode_fps\":30.000") != std::string::npos);
  CHECK(line.find("\"fingerprints_verified\":false") !=
        std::string::npos);
  CHECK(line.find("\"asset_sha256\":null") != std::string::npos);
  CHECK(line.find(
            "\"source_sha256\":[null,null,null,null,null,null]") !=
        std::string::npos);
}
