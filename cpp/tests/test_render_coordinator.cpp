#include "test_support.hpp"

#include <swim/core/backend.hpp>
#include <swim/core/benchmark_stage.hpp>
#include <swim/core/render_coordinator.hpp>
#include <swim/core/run_lifecycle.hpp>

#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <stdexcept>
#include <thread>
#include <utility>
#include <vector>

namespace {

using namespace std::chrono_literals;
constexpr std::uint32_t kTestBackendTag = 0x54455354;

struct MockNative {
  std::atomic_int references{1};
};

void retain(void* pointer) noexcept {
  static_cast<MockNative*>(pointer)->references.fetch_add(
      1, std::memory_order_relaxed);
}

void release(void* pointer) noexcept {
  auto* native = static_cast<MockNative*>(pointer);
  if (native->references.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    delete native;
  }
}

swim::core::FrameLease mock_frame(
    std::uint32_t camera, std::uint64_t sequence,
    std::chrono::steady_clock::time_point arrived_at) {
  swim::core::FrameMetadata metadata{};
  metadata.camera_index = camera;
  metadata.sequence = sequence;
  metadata.arrived_at = arrived_at;
  return swim::core::FrameLease{
      new MockNative,
      swim::core::NativeLeaseOps{retain, release, kTestBackendTag}, metadata};
}

class FakeRenderer final : public swim::core::IRenderer {
 public:
  swim::core::RenderSubmitResult submit(
      const swim::core::RenderSnapshot& snapshot) override {
    if (capture_snapshots) {
      snapshots.push_back(snapshot);
    }
    submit_count.fetch_add(1, std::memory_order_relaxed);
    if (!accept_submissions ||
        submit_count.load(std::memory_order_relaxed) <= reject_first) {
      return result == swim::core::RenderSubmitResult::accepted
                 ? swim::core::RenderSubmitResult::backpressure
                 : result;
    }
    return result;
  }

  swim::core::FrameLease replacement_frame(
      std::uint32_t camera_index) const override {
    return mock_frame(camera_index, 10'000 + camera_index, {});
  }

  swim::core::FrameLease benchmark_frame(
      std::uint32_t camera_index) const override {
    return mock_frame(camera_index, 20'000 + camera_index, {});
  }

  void drain() override {}

  bool accept_submissions{true};
  bool capture_snapshots{true};
  std::size_t reject_first{};
  swim::core::RenderSubmitResult result{
      swim::core::RenderSubmitResult::accepted};
  std::atomic_uint64_t submit_count{};
  std::vector<swim::core::RenderSnapshot> snapshots;
};

struct CoordinatorFixture {
  explicit CoordinatorFixture(FakeRenderer& renderer,
                              swim::core::AppConfig initial_config = {},
                              swim::core::BenchmarkGraph initial_graph =
                                  {6, true, false, false, false})
      : config(std::move(initial_config)),
        graph(initial_graph),
        lifecycle(config.duration),
        coordinator(mailboxes, renderer, config, graph, metrics, lifecycle) {}

  void publish(std::uint32_t camera, std::uint64_t sequence,
               std::chrono::steady_clock::time_point arrived_at) {
    mailboxes[camera].publish(mock_frame(camera, sequence, arrived_at));
  }

  void publish_all(std::uint64_t sequence,
                   std::chrono::steady_clock::time_point arrived_at) {
    for (std::uint32_t camera = 0; camera < mailboxes.size(); ++camera) {
      publish(camera, sequence, arrived_at);
    }
  }

  std::array<swim::core::LatestFrameMailbox, 6> mailboxes;
  swim::core::AppConfig config;
  swim::core::BenchmarkGraph graph;
  swim::core::RuntimeCounters metrics;
  swim::core::RunLifecycle lifecycle;
  swim::core::RenderCoordinator coordinator;
};

}  // namespace

TEST_CASE(coordinator_reuses_stale_camera_without_waiting) {
  const auto t0 = std::chrono::steady_clock::now();
  FakeRenderer renderer;
  CoordinatorFixture fixture{renderer};
  fixture.publish_all(1, t0);
  fixture.coordinator.tick(t0);
  fixture.publish(0, 2, t0 + 33ms);
  fixture.coordinator.tick(t0 + 33ms);

  CHECK_EQ(renderer.snapshots.size(), 2u);
  CHECK_EQ(renderer.snapshots[1].frames[0].metadata().sequence, 2u);
  for (std::size_t camera = 1; camera < 6; ++camera) {
    CHECK_EQ(renderer.snapshots[1].frames[camera].metadata().sequence, 1u);
  }
  CHECK_EQ(fixture.metrics.reused.load(std::memory_order_relaxed), 5u);
}

TEST_CASE(coordinator_replaces_camera_only_after_one_second) {
  const auto t0 = std::chrono::steady_clock::now();
  FakeRenderer renderer;
  CoordinatorFixture fixture{renderer};
  fixture.publish_all(7, t0);
  fixture.coordinator.tick(t0);
  fixture.coordinator.tick(t0 + 999ms);
  fixture.coordinator.tick(t0 + 1001ms);

  CHECK_EQ(renderer.snapshots[1].frames[2].metadata().sequence, 7u);
  CHECK_EQ(renderer.snapshots[2].frames[2].metadata().sequence, 10'002u);
}

TEST_CASE(coordinator_submits_once_and_counts_backpressure_drop) {
  const auto now = std::chrono::steady_clock::now();
  FakeRenderer renderer;
  renderer.accept_submissions = false;
  CoordinatorFixture fixture{renderer};
  fixture.publish_all(1, now);
  fixture.coordinator.tick(now);

  CHECK_EQ(renderer.snapshots.size(), 1u);
  CHECK_EQ(fixture.metrics.render_submissions.load(std::memory_order_relaxed),
           0u);
  CHECK_EQ(fixture.metrics.render_drops.load(std::memory_order_relaxed), 1u);
}

TEST_CASE(coordinator_distinguishes_recoverable_and_fatal_submit_results) {
  const auto now = std::chrono::steady_clock::now();
  for (const auto recoverable : {swim::core::RenderSubmitResult::not_ready,
                                 swim::core::RenderSubmitResult::backpressure}) {
    FakeRenderer renderer;
    renderer.result = recoverable;
    CoordinatorFixture fixture{renderer};
    fixture.publish_all(1, now);
    CHECK_EQ(fixture.coordinator.tick(now), recoverable);
    CHECK_EQ(fixture.metrics.render_drops.load(), 1u);
  }
  for (const auto unrecoverable : {swim::core::RenderSubmitResult::fatal,
                                   swim::core::RenderSubmitResult::invalid}) {
    FakeRenderer renderer;
    renderer.result = unrecoverable;
    CoordinatorFixture fixture{renderer};
    fixture.publish_all(1, now);
    bool threw = false;
    try {
      static_cast<void>(fixture.coordinator.tick(now));
    } catch (const std::runtime_error&) {
      threw = true;
    }
    CHECK(threw);
  }
}

TEST_CASE(coordinator_counts_real_sequence_gap_after_replacement) {
  const auto t0 = std::chrono::steady_clock::now();
  FakeRenderer renderer;
  CoordinatorFixture fixture{renderer};
  fixture.publish_all(7, t0);
  fixture.coordinator.tick(t0);
  fixture.coordinator.tick(t0 + 1001ms);
  fixture.publish(0, 12, t0 + 1002ms);
  fixture.coordinator.tick(t0 + 1002ms);
  CHECK_EQ(fixture.metrics.overwritten.load(), 4u);
}

TEST_CASE(coordinator_finite_duration_starts_at_first_accepted_submit) {
  FakeRenderer renderer;
  renderer.reject_first = 3;
  swim::core::AppConfig config;
  config.fps_num = 100;
  config.fps_den = 1;
  config.duration = 1s;
  CoordinatorFixture fixture{renderer, std::move(config)};
  fixture.coordinator.run({});

  CHECK(renderer.snapshots.size() >= 103u);
  CHECK(fixture.metrics.render_drops.load(std::memory_order_relaxed) >= 3u);
  CHECK(fixture.metrics.render_submissions.load(std::memory_order_relaxed) >=
        100u);
  CHECK(fixture.metrics.render_active_ns.load(std::memory_order_relaxed) >=
        990'000'000u);
}

TEST_CASE(coordinator_zero_duration_runs_until_stop_is_requested) {
  FakeRenderer renderer;
  swim::core::AppConfig config;
  config.mode = swim::core::RunMode::benchmark;
  config.duration = 0s;
  CoordinatorFixture fixture{renderer, std::move(config)};
  std::jthread worker([&](std::stop_token token) {
    fixture.coordinator.run(token);
  });
  while (renderer.submit_count.load(std::memory_order_relaxed) < 10u) {
    std::this_thread::yield();
  }
  worker.request_stop();
  worker.join();
  CHECK(renderer.snapshots.size() >= 10u);
}

TEST_CASE(coordinator_uses_exact_integer_rational_cadence) {
  CHECK_EQ(swim::core::RenderCoordinator::cadence_offset(1, 30'000, 1'001),
           33'366'666ns);
  CHECK_EQ(swim::core::RenderCoordinator::cadence_offset(2, 30'000, 1'001),
           66'733'333ns);
  CHECK_EQ(swim::core::RenderCoordinator::cadence_offset(3, 30'000, 1'001),
           100'100'000ns);
  CHECK_EQ(swim::core::RenderCoordinator::cadence_offset(30'000, 30'000,
                                                         1'001),
           1'001s);
}

TEST_CASE(coordinator_first_accepted_submit_activates_global_lifecycle) {
  const auto t0 = std::chrono::steady_clock::now();
  FakeRenderer renderer;
  renderer.reject_first = 1;
  CoordinatorFixture fixture{renderer};
  fixture.publish_all(1, t0);
  CHECK_EQ(fixture.coordinator.tick(t0),
           swim::core::RenderSubmitResult::backpressure);
  CHECK(!fixture.lifecycle.active());
  CHECK_EQ(fixture.coordinator.tick(t0 + 1ms),
           swim::core::RenderSubmitResult::accepted);
  CHECK(fixture.lifecycle.active());
  CHECK(fixture.lifecycle.active_started_at() >= t0);
  CHECK_EQ(fixture.lifecycle.deadline(),
           fixture.lifecycle.active_started_at() + fixture.config.duration);
}

TEST_CASE(coordinator_render_only_seeds_all_six_resident_frames) {
  FakeRenderer renderer;
  swim::core::AppConfig config;
  config.stage = swim::core::BenchmarkStage::render_only;
  const auto graph = swim::core::resolve_benchmark_graph(config);
  CoordinatorFixture fixture{renderer, std::move(config), graph};

  CHECK_EQ(fixture.coordinator.tick(std::chrono::steady_clock::now()),
           swim::core::RenderSubmitResult::accepted);
  CHECK_EQ(renderer.snapshots.size(), 1u);
  for (std::size_t camera = 0; camera < 6; ++camera) {
    CHECK_EQ(renderer.snapshots[0].frames[camera].metadata().sequence,
             20'000u + camera);
  }
}

TEST_CASE(coordinator_inactive_lanes_ignore_mailboxes_and_remain_resident) {
  const auto now = std::chrono::steady_clock::now();
  FakeRenderer renderer;
  swim::core::AppConfig config;
  config.stream_count = 1;
  config.stage = swim::core::BenchmarkStage::decode_render;
  const auto graph = swim::core::resolve_benchmark_graph(config);
  CoordinatorFixture fixture{renderer, std::move(config), graph};
  fixture.publish(0, 7, now);
  fixture.publish(3, 99, now);

  CHECK_EQ(fixture.coordinator.tick(now),
           swim::core::RenderSubmitResult::accepted);
  CHECK_EQ(renderer.snapshots[0].frames[0].metadata().sequence, 7u);
  CHECK_EQ(renderer.snapshots[0].frames[3].metadata().sequence, 20'003u);
  swim::core::FrameLease ignored;
  CHECK(fixture.mailboxes[3].consume_latest(ignored));
  CHECK_EQ(ignored.metadata().sequence, 99u);
}

TEST_CASE(coordinator_benchmark_mode_is_not_cadence_limited) {
  FakeRenderer renderer;
  renderer.capture_snapshots = false;
  swim::core::AppConfig config;
  config.mode = swim::core::RunMode::benchmark;
  config.stage = swim::core::BenchmarkStage::render_only;
  config.fps_num = 1;
  config.fps_den = 1;
  config.duration = 0s;
  const auto graph = swim::core::resolve_benchmark_graph(config);
  CoordinatorFixture fixture{renderer, std::move(config), graph};

  std::jthread worker(
      [&](std::stop_token token) { fixture.coordinator.run(token); });
  const auto timeout = std::chrono::steady_clock::now() + 200ms;
  while (renderer.submit_count.load(std::memory_order_relaxed) <= 10u &&
         std::chrono::steady_clock::now() < timeout) {
    std::this_thread::yield();
  }
  worker.request_stop();
  worker.join();

  CHECK(renderer.submit_count.load(std::memory_order_relaxed) > 10u);
  CHECK(fixture.metrics.render_submissions.load(std::memory_order_relaxed) >
        10u);
}
