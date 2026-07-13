#pragma once

#include <swim/core/asset.hpp>
#include <swim/core/config.hpp>
#include <swim/core/frame.hpp>
#include <swim/core/latest_frame_mailbox.hpp>
#include <swim/core/metrics.hpp>
#include <swim/core/run_lifecycle.hpp>

#include <array>
#include <chrono>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <stop_token>
#include <string>
#include <string_view>

namespace swim::core {

struct BenchmarkGraph;

struct BackendRuntimeSample final {
  std::optional<std::uint64_t> gpu_allocated_bytes;
};

class ISource {
 public:
  virtual ~ISource() = default;
  virtual void start(LatestFrameMailbox& output) = 0;
  virtual void stop() noexcept = 0;
  virtual bool failed() const noexcept { return false; }
  virtual std::string last_error() const { return {}; }
};

struct RenderSnapshot {
  std::array<FrameLease, 6> frames;
  std::chrono::steady_clock::time_point sampled_at;
};

enum class RenderSubmitResult : std::uint8_t {
  accepted,
  not_ready,
  backpressure,
  fatal,
  invalid,
};

class IRenderer {
 public:
  virtual ~IRenderer() = default;
  virtual RenderSubmitResult submit(const RenderSnapshot& snapshot) = 0;
  virtual FrameLease replacement_frame(
      std::uint32_t camera_index) const = 0;
  virtual FrameLease benchmark_frame(
      std::uint32_t camera_index) const = 0;
  virtual void drain() = 0;
  virtual bool has_fatal_error() const noexcept { return false; }
  virtual std::string last_error() const { return {}; }
};

class IBackend {
 public:
  virtual ~IBackend() = default;
  virtual std::unique_ptr<ISource> make_source(const SourceConfig& config,
                                               std::uint32_t camera_index) = 0;
  virtual std::unique_ptr<IRenderer> make_renderer(
      const RuntimeAsset& asset, const AppConfig& config,
      const BenchmarkGraph& graph) = 0;
  virtual void bind_metrics(RuntimeCounters&) noexcept {}
  virtual void bind_lifecycle(RunLifecycle&) noexcept {}
  virtual BackendRuntimeSample sample_runtime() const noexcept { return {}; }
  virtual void run_main_loop(std::stop_token token) = 0;
  virtual void stop_main_loop() noexcept = 0;
};

using SourceArray = std::array<std::unique_ptr<ISource>, 6>;
using MailboxArray = std::array<LatestFrameMailbox, 6>;

using BackendFactory = std::unique_ptr<IBackend> (*)();

class BackendRegistry {
 public:
  static BackendRegistry& instance();

  void register_factory(std::string name, BackendFactory factory);
  std::unique_ptr<IBackend> create(std::string_view name) const;

 private:
  mutable std::mutex mutex_;
  std::map<std::string, BackendFactory, std::less<>> factories_;
};

}  // namespace swim::core
