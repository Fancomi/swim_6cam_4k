#pragma once

#include <swim/core/backend.hpp>
#include <swim/core/benchmark_stage.hpp>
#include <swim/core/config.hpp>
#include <swim/core/metrics.hpp>

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

namespace swim::core {

struct BenchmarkReporterMetadata final {
  AppConfig config;
  BenchmarkGraph graph;
  std::optional<BenchmarkManifest> manifest;
  std::uint32_t output_width{};
  std::uint32_t output_height{};
  std::string generated_run_id;
};

using BenchmarkWriteOperation =
    std::function<std::ptrdiff_t(int, const void*, std::size_t)>;

std::string serialize_benchmark_record(
    const BenchmarkReporterMetadata& metadata,
    const MetricsSnapshot& current,
    const MetricsSnapshot& previous,
    BackendRuntimeSample backend,
    double elapsed_seconds,
    bool final,
    std::size_t healthy_sources,
    std::optional<std::uint64_t> rss_bytes);

class BenchmarkReporter final {
 public:
  BenchmarkReporter(BenchmarkReporterMetadata metadata,
                    RuntimeCounters& metrics,
                    BenchmarkWriteOperation writer = {});
  ~BenchmarkReporter();
  BenchmarkReporter(const BenchmarkReporter&) = delete;
  BenchmarkReporter& operator=(const BenchmarkReporter&) = delete;

  void bind_backend(const IBackend& backend) noexcept;
  void unbind_backend() noexcept;
  void start();
  void stop_intervals();
  void write_interval_now();
  void write_final(std::size_t healthy_sources);

 private:
  void write_record(const MetricsSnapshot& current,
                    const MetricsSnapshot& previous,
                    BackendRuntimeSample backend,
                    double elapsed_seconds,
                    bool final,
                    std::size_t healthy_sources);
  void join_intervals() noexcept;
  void remember_background_error(std::exception_ptr error) noexcept;
  std::exception_ptr background_error() const noexcept;

  BenchmarkReporterMetadata metadata_;
  RuntimeCounters& metrics_;
  BenchmarkWriteOperation writer_;
  const IBackend* backend_{};
  int output_fd_{-1};
  std::chrono::steady_clock::time_point started_at_;
  std::chrono::steady_clock::time_point previous_at_;
  std::optional<MetricsSnapshot> previous_;
  std::jthread thread_;
  std::mutex wait_mutex_;
  std::condition_variable_any wait_condition_;
  mutable std::mutex error_mutex_;
  std::exception_ptr background_error_;
  bool started_{};
  bool final_attempted_{};
};

}  // namespace swim::core
