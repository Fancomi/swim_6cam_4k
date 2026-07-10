#include "test_support.hpp"

#include <swim/core/backend.hpp>
#include <swim/core/runtime_start.hpp>

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <stop_token>
#include <string>
#include <thread>

namespace {

class NullSource final : public swim::core::ISource {
 public:
  void start(swim::core::LatestFrameMailbox& output) override {
    output_ = &output;
  }

  void stop() noexcept override { output_ = nullptr; }

 private:
  swim::core::LatestFrameMailbox* output_{};
};

class ThrowingStartSource final : public swim::core::ISource {
 public:
  explicit ThrowingStartSource(bool throws) : throws_(throws) {}
  void start(swim::core::LatestFrameMailbox&) override {
    if (throws_) {
      throw std::runtime_error("injected source start failure");
    }
    started_ = true;
  }
  void stop() noexcept override { stopped_ = true; }
  bool started_{};
  bool stopped_{};

 private:
  bool throws_{};
};

class NullRenderer final : public swim::core::IRenderer {
 public:
  swim::core::RenderSubmitResult submit(
      const swim::core::RenderSnapshot&) override {
    return swim::core::RenderSubmitResult::accepted;
  }

  swim::core::FrameLease replacement_frame(
      std::uint32_t) const override {
    return {};
  }

  void drain() override {}
};

class NullBackend final : public swim::core::IBackend {
 public:
  std::unique_ptr<swim::core::ISource> make_source(
      const swim::core::SourceConfig&, std::uint32_t) override {
    return std::make_unique<NullSource>();
  }

  std::unique_ptr<swim::core::IRenderer> make_renderer(
      const swim::core::RuntimeAsset&,
      const swim::core::AppConfig&) override {
    return std::make_unique<NullRenderer>();
  }

  void run_main_loop(std::stop_token token) override {
    std::unique_lock lock(mutex_);
    condition_.wait(lock, token, [this] { return stopped_; });
  }

  void stop_main_loop() noexcept override {
    {
      std::lock_guard lock(mutex_);
      stopped_ = true;
    }
    condition_.notify_all();
  }

 private:
  std::mutex mutex_;
  std::condition_variable_any condition_;
  bool stopped_{};
};

std::unique_ptr<swim::core::IBackend> make_null_backend() {
  return std::make_unique<NullBackend>();
}

}  // namespace

TEST_CASE(registry_creates_a_backend_that_implements_every_contract) {
  swim::core::BackendRegistry registry;
  registry.register_factory("null", &make_null_backend);
  auto backend = registry.create("null");

  swim::core::AppConfig config;
  auto source = backend->make_source(config.sources[0], 0);
  swim::core::LatestFrameMailbox mailbox;
  source->start(mailbox);
  source->stop();

  swim::core::RuntimeAsset asset{};
  auto renderer = backend->make_renderer(asset, config);
  swim::core::RenderSnapshot snapshot{};
  CHECK_EQ(renderer->submit(snapshot),
           swim::core::RenderSubmitResult::accepted);
  CHECK(!renderer->replacement_frame(0));
  renderer->drain();

  std::atomic<bool> exited{};
  std::jthread loop([&](std::stop_token token) {
    backend->run_main_loop(token);
    exited.store(true, std::memory_order_release);
  });
  backend->stop_main_loop();
  loop.join();
  CHECK(exited.load(std::memory_order_acquire));
}

TEST_CASE(null_main_loop_also_exits_when_its_stop_token_is_requested) {
  swim::core::BackendRegistry registry;
  registry.register_factory("null", &make_null_backend);
  auto backend = registry.create("null");

  std::atomic<bool> exited{};
  std::jthread loop([&](std::stop_token token) {
    backend->run_main_loop(token);
    exited.store(true, std::memory_order_release);
  });
  loop.request_stop();
  loop.join();
  CHECK(exited.load(std::memory_order_acquire));
}

TEST_CASE(registry_reports_registered_backend_names_in_sorted_order) {
  swim::core::BackendRegistry registry;
  registry.register_factory("zeta", &make_null_backend);
  registry.register_factory("alpha", &make_null_backend);
  registry.register_factory("middle", &make_null_backend);

  CHECK_THROWS_WITH(
      registry.create("missing"),
      "unknown backend 'missing'; registered backends: alpha,middle,zeta");
}

TEST_CASE(registry_rejects_duplicate_names_and_null_factories) {
  swim::core::BackendRegistry registry;
  registry.register_factory("null", &make_null_backend);
  CHECK_THROWS_WITH(registry.register_factory("null", &make_null_backend),
                    "backend 'null' is already registered");
  CHECK_THROWS_WITH(registry.register_factory("empty", nullptr),
                    "backend factory for 'empty' is null");
}

TEST_CASE(global_backend_registry_is_stable) {
  CHECK(&swim::core::BackendRegistry::instance() ==
        &swim::core::BackendRegistry::instance());
}

TEST_CASE(recorded_source_start_stops_all_and_excludes_throwing_lane) {
  std::array<std::unique_ptr<swim::core::ISource>, 6> sources;
  std::array<ThrowingStartSource*, 6> views{};
  for (std::size_t camera = 0; camera < sources.size(); ++camera) {
    auto source = std::make_unique<ThrowingStartSource>(camera == 2);
    views[camera] = source.get();
    sources[camera] = std::move(source);
  }
  std::array<swim::core::LatestFrameMailbox, 6> mailboxes;
  swim::core::RuntimeStartState state;
  CHECK_THROWS_WITH(
      swim::core::start_sources_recorded(sources, mailboxes, state),
      "injected source start failure");
  CHECK_EQ(state.started_count(), 2u);
  for (const auto* source : views) {
    CHECK(source->stopped_);
  }
}
