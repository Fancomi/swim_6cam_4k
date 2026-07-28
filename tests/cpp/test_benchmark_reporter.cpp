#include "test_support.hpp"

#include <swim/core/benchmark_reporter.hpp>
#include <swim/core/benchmark_stage.hpp>

#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <string>
#include <thread>

#if defined(_WIN32)
#include <io.h>
#else
#include <unistd.h>
#endif

using namespace std::chrono_literals;

namespace {

// Descriptor write and a writable scratch path differ by platform; keep the
// test bodies neutral by resolving both through these helpers.
std::ptrdiff_t os_write(int fd, const void* bytes, std::size_t size) {
#if defined(_WIN32)
  return static_cast<std::ptrdiff_t>(
      _write(fd, bytes, static_cast<unsigned int>(size)));
#else
  return static_cast<std::ptrdiff_t>(::write(fd, bytes, size));
#endif
}

std::filesystem::path scratch_path(const char* name) {
  return std::filesystem::temp_directory_path() / name;
}

// MSVC will not bind an empty braced temporary to the const MetricsSnapshot&
// parameter, so materialize an all-zero snapshot from default counters.
swim::core::MetricsSnapshot zero_snapshot() {
  swim::core::RuntimeCounters counters;
  return counters.sample_totals();
}

swim::core::BenchmarkReporterMetadata reporter_metadata() {
  swim::core::AppConfig config;
  config.backend = "metal\"test";
  config.mode = swim::core::RunMode::benchmark;
  config.stage = swim::core::BenchmarkStage::decode_render_encode;
  config.stream_count = 6;
  config.preview = false;
  config.encode = true;
  config.metrics_path = scratch_path("swim_benchmark_reporter_test.jsonl");

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
      reporter_metadata(), counters.sample_totals(), zero_snapshot(), {123456},
      2.0, false, 6, 987654);

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
  std::filesystem::remove(scratch_path("swim_benchmark_reporter_test.jsonl"));
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
      metadata, counters.sample_totals(), zero_snapshot(), {}, 10.0, true, 6,
      1);
  CHECK(line.find("\"render_fps\":30.000") != std::string::npos);
  CHECK(line.find("\"encode_fps\":30.000") != std::string::npos);
  CHECK(line.find("\"fingerprints_verified\":false") !=
        std::string::npos);
  CHECK(line.find("\"asset_sha256\":null") != std::string::npos);
  CHECK(line.find(
            "\"source_sha256\":[null,null,null,null,null,null]") !=
        std::string::npos);
}

TEST_CASE(schema_reports_unavailable_process_and_gpu_resources_as_null) {
  const auto line = swim::core::serialize_benchmark_record(
      reporter_metadata(), zero_snapshot(), zero_snapshot(), {}, 1.0, true, 0,
      std::nullopt);
  CHECK(line.find("\"gpu_allocated_bytes\":null") != std::string::npos);
  CHECK(line.find("\"rss_bytes\":null") != std::string::npos);

  const auto measured_zero = swim::core::serialize_benchmark_record(
      reporter_metadata(), zero_snapshot(), zero_snapshot(), {0}, 1.0, true, 0,
      std::optional<std::uint64_t>{0});
  CHECK(measured_zero.find("\"gpu_allocated_bytes\":0") !=
        std::string::npos);
  CHECK(measured_zero.find("\"rss_bytes\":0") != std::string::npos);
}

TEST_CASE(background_partial_write_propagates_and_final_is_attempted_once) {
  std::filesystem::remove(scratch_path("swim_benchmark_reporter_test.jsonl"));
  swim::core::RuntimeCounters counters;
  std::atomic_uint32_t writes{};
  std::string partial;
  auto writer = [&](int, const void* bytes, std::size_t size) -> std::ptrdiff_t {
    const auto call = writes.fetch_add(1, std::memory_order_relaxed);
    if (call == 0) {
      partial.assign(static_cast<const char*>(bytes), size - 1);
      return static_cast<std::ptrdiff_t>(size - 1);
    }
    return static_cast<std::ptrdiff_t>(size);
  };
  swim::core::BenchmarkReporter reporter{reporter_metadata(), counters,
                                          writer};
  reporter.start();
  const auto deadline = std::chrono::steady_clock::now() + 2s;
  while (writes.load(std::memory_order_relaxed) == 0 &&
         std::chrono::steady_clock::now() < deadline) {
    std::this_thread::sleep_for(1ms);
  }

  CHECK_EQ(writes.load(std::memory_order_relaxed), 1u);
  CHECK(!partial.ends_with('\n'));
  CHECK_THROWS_WITH(reporter.stop_intervals(),
                    "benchmark metrics write was partial");
  CHECK_THROWS_WITH(reporter.write_final(0),
                    "benchmark metrics write was partial");
  CHECK_EQ(writes.load(std::memory_order_relaxed), 1u);
  reporter.write_final(0);
  CHECK_EQ(writes.load(std::memory_order_relaxed), 1u);
}

TEST_CASE(partial_file_write_is_rolled_back_before_output_is_poisoned) {
  const auto path = std::filesystem::path{
      scratch_path("swim_benchmark_reporter_partial_test.jsonl")};
  std::filesystem::remove(path);
  auto metadata = reporter_metadata();
  metadata.config.metrics_path = path;
  swim::core::RuntimeCounters counters;
  auto partial_writer = [](int fd, const void* bytes,
                           std::size_t size) -> std::ptrdiff_t {
    return os_write(fd, bytes, size - 1);
  };
  {
    swim::core::BenchmarkReporter reporter{std::move(metadata), counters,
                                            partial_writer};
    CHECK_THROWS_WITH(reporter.write_interval_now(),
                      "benchmark metrics write was partial");
    CHECK_THROWS_WITH(reporter.write_final(0),
                      "benchmark metrics write was partial");
  }
  CHECK_EQ(std::filesystem::file_size(path), 0u);
}

TEST_CASE(negative_final_write_is_propagated_and_never_retried) {
  std::filesystem::remove(scratch_path("swim_benchmark_reporter_test.jsonl"));
  swim::core::RuntimeCounters counters;
  std::uint32_t writes = 0;
  auto failed_writer = [&](int, const void*, std::size_t) -> std::ptrdiff_t {
    ++writes;
    errno = EIO;
    return -1;
  };
  swim::core::BenchmarkReporter reporter{reporter_metadata(), counters,
                                          failed_writer};
  CHECK_THROWS_WITH(reporter.write_final(0),
                    "benchmark metrics write failed: " +
                        std::to_string(EIO));
  CHECK_EQ(writes, 1u);
  reporter.write_final(0);
  CHECK_EQ(writes, 1u);
}

TEST_CASE(reporter_can_drop_backend_borrow_before_backend_destruction) {
  std::filesystem::remove(scratch_path("swim_benchmark_reporter_test.jsonl"));
  swim::core::RuntimeCounters counters;
  std::uint32_t writes = 0;
  auto writer = [&](int, const void*, std::size_t size) -> std::ptrdiff_t {
    ++writes;
    return static_cast<std::ptrdiff_t>(size);
  };
  swim::core::BenchmarkReporter reporter{reporter_metadata(), counters,
                                          writer};
  {
    SampleBackend backend;
    reporter.bind_backend(backend);
    reporter.unbind_backend();
  }
  reporter.write_final(0);
  CHECK_EQ(writes, 1u);
}
