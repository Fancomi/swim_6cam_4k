#pragma once

#include <swim/core/asset.hpp>
#include <swim/core/config.hpp>
#include <swim/core/frame.hpp>
#include <swim/core/latest_frame_mailbox.hpp>

#include <array>
#include <chrono>
#include <cstdint>
#include <map>
#include <memory>
#include <mutex>
#include <stop_token>
#include <string>
#include <string_view>

namespace swim::core {

class ISource {
 public:
  virtual ~ISource() = default;
  virtual void start(LatestFrameMailbox& output) = 0;
  virtual void stop() noexcept = 0;
};

struct RenderSnapshot {
  std::array<FrameLease, 6> frames;
  std::chrono::steady_clock::time_point sampled_at;
};

class IRenderer {
 public:
  virtual ~IRenderer() = default;
  virtual bool submit(const RenderSnapshot& snapshot) = 0;
  virtual FrameLease replacement_frame(
      std::uint32_t camera_index) const = 0;
  virtual void drain() = 0;
};

class IBackend {
 public:
  virtual ~IBackend() = default;
  virtual std::unique_ptr<ISource> make_source(const SourceConfig& config,
                                               std::uint32_t camera_index) = 0;
  virtual std::unique_ptr<IRenderer> make_renderer(
      const RuntimeAsset& asset, const AppConfig& config) = 0;
  virtual void run_main_loop(std::stop_token token) = 0;
  virtual void stop_main_loop() noexcept = 0;
};

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
