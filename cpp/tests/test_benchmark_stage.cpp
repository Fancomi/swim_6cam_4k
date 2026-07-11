#include "test_support.hpp"

#include <swim/core/benchmark_stage.hpp>
#include <swim/core/runtime_start.hpp>

#include <array>
#include <chrono>
#include <cstdint>
#include <memory>
#include <stop_token>
#include <thread>

namespace {

using namespace std::chrono_literals;

void retain_static(void*) noexcept {}
void release_static(void*) noexcept {}

swim::core::FrameLease static_frame(std::uint32_t camera) {
  static std::array<std::uint32_t, 6> storage{};
  swim::core::FrameMetadata metadata{};
  metadata.camera_index = camera;
  metadata.width = 3840;
  metadata.height = 2160;
  return {&storage[camera], {retain_static, release_static, 0x54455354},
          metadata};
}

class FakeSource final : public swim::core::ISource {
 public:
  explicit FakeSource(std::uint32_t camera) : camera_(camera) {}

  void start(swim::core::LatestFrameMailbox& output) override {
    started = true;
    if (publish_on_start) {
      output.publish(static_frame(camera_));
    }
  }
  void stop() noexcept override { stopped = true; }
  bool failed() const noexcept override { return failure; }

  bool publish_on_start{};
  bool failure{};
  bool started{};
  bool stopped{};

 private:
  std::uint32_t camera_{};
};

class CountingBackend final : public swim::core::IBackend {
 public:
  std::unique_ptr<swim::core::ISource> make_source(
      const swim::core::SourceConfig&, std::uint32_t camera) override {
    ++source_calls;
    auto source = std::make_unique<FakeSource>(camera);
    views[camera] = source.get();
    return source;
  }

  std::unique_ptr<swim::core::IRenderer> make_renderer(
      const swim::core::RuntimeAsset&,
      const swim::core::AppConfig&,
      const swim::core::BenchmarkGraph&) override {
    ++renderer_calls;
    return {};
  }

  void run_main_loop(std::stop_token) override {}
  void stop_main_loop() noexcept override {}

  std::array<FakeSource*, 6> views{};
  std::uint32_t source_calls{};
  std::uint32_t renderer_calls{};
};

swim::core::AppConfig config_for(swim::core::BenchmarkStage stage,
                                 std::uint32_t stream_count) {
  swim::core::AppConfig config;
  config.stage = stage;
  config.stream_count = stream_count;
  config.preview = true;
  config.encode = true;
  return config;
}

}  // namespace

TEST_CASE(benchmark_stage_resolves_real_components) {
  using swim::core::BenchmarkStage;
  using swim::core::resolve_benchmark_graph;

  const auto decode =
      resolve_benchmark_graph(config_for(BenchmarkStage::decode_only, 2));
  CHECK_EQ(decode.active_sources, 2u);
  CHECK(!decode.create_renderer);
  CHECK(!decode.synthetic_inputs);
  CHECK(!decode.preview);
  CHECK(!decode.encode);

  const auto render =
      resolve_benchmark_graph(config_for(BenchmarkStage::render_only, 4));
  CHECK_EQ(render.active_sources, 0u);
  CHECK(render.create_renderer);
  CHECK(render.synthetic_inputs);
  CHECK(!render.preview);
  CHECK(!render.encode);

  const auto encode = resolve_benchmark_graph(
      config_for(BenchmarkStage::decode_render_encode, 6));
  CHECK_EQ(encode.active_sources, 6u);
  CHECK(encode.create_renderer);
  CHECK(!encode.synthetic_inputs);
  CHECK(!encode.preview);
  CHECK(encode.encode);
}

TEST_CASE(benchmark_stage_output_policy_is_authoritative) {
  using swim::core::BenchmarkStage;
  using swim::core::resolve_benchmark_graph;

  auto full_config = config_for(BenchmarkStage::full, 1);
  full_config.preview = false;
  full_config.encode = true;
  const auto full = resolve_benchmark_graph(full_config);
  CHECK_EQ(full.active_sources, 1u);
  CHECK(full.create_renderer);
  CHECK(!full.synthetic_inputs);
  CHECK(!full.preview);
  CHECK(full.encode);

  const auto preview = resolve_benchmark_graph(
      config_for(BenchmarkStage::decode_render_preview, 2));
  CHECK(preview.preview);
  CHECK(!preview.encode);

  const auto encode = resolve_benchmark_graph(
      config_for(BenchmarkStage::decode_render_encode, 2));
  CHECK(!encode.preview);
  CHECK(encode.encode);

  const auto render = resolve_benchmark_graph(
      config_for(BenchmarkStage::decode_render, 2));
  CHECK(!render.preview);
  CHECK(!render.encode);
}

TEST_CASE(benchmark_stage_rejects_unsupported_stream_counts) {
  using swim::core::BenchmarkStage;
  using swim::core::resolve_benchmark_graph;

  for (const auto count : std::array<std::uint32_t, 4>{0, 3, 5, 7}) {
    CHECK_THROWS_WITH(
        resolve_benchmark_graph(config_for(BenchmarkStage::full, count)),
        "stream_count must be one of 1, 2, 4, or 6");
  }
}

TEST_CASE(benchmark_stage_and_pacing_names_are_stable) {
  using swim::core::BenchmarkStage;
  using swim::core::RunMode;

  CHECK_EQ(swim::core::benchmark_stage_name(BenchmarkStage::full), "full");
  CHECK_EQ(swim::core::benchmark_stage_name(BenchmarkStage::decode_only),
           "decode-only");
  CHECK_EQ(swim::core::benchmark_stage_name(BenchmarkStage::render_only),
           "render-only");
  CHECK_EQ(swim::core::benchmark_stage_name(BenchmarkStage::decode_render),
           "decode-render");
  CHECK_EQ(swim::core::benchmark_stage_name(
               BenchmarkStage::decode_render_preview),
           "decode-render-preview");
  CHECK_EQ(swim::core::benchmark_stage_name(
               BenchmarkStage::decode_render_encode),
           "decode-render-encode");
  CHECK_EQ(swim::core::pacing_name(RunMode::realtime), "realtime");
  CHECK_EQ(swim::core::pacing_name(RunMode::benchmark), "benchmark");
}

TEST_CASE(benchmark_stage_creates_and_starts_only_active_sources) {
  CountingBackend backend;
  swim::core::AppConfig config;
  auto sources = swim::core::make_sources(backend, config, 2);
  CHECK_EQ(backend.source_calls, 2u);
  CHECK(sources[0]);
  CHECK(sources[1]);
  for (std::size_t camera = 2; camera < sources.size(); ++camera) {
    CHECK(!sources[camera]);
  }

  std::array<swim::core::LatestFrameMailbox, 6> mailboxes;
  swim::core::RuntimeStartState state;
  swim::core::start_sources_recorded(sources, mailboxes, state);
  CHECK_EQ(state.started_count(), 2u);
  swim::core::stop_sources(sources);
  CHECK(backend.views[0]->stopped);
  CHECK(backend.views[1]->stopped);
}

TEST_CASE(decode_only_lifecycle_starts_on_publication_and_runs_for_duration) {
  CountingBackend backend;
  swim::core::AppConfig config;
  auto sources = swim::core::make_sources(backend, config, 1);
  backend.views[0]->publish_on_start = true;
  std::array<swim::core::LatestFrameMailbox, 6> mailboxes;
  swim::core::RuntimeStartState state;
  swim::core::start_sources_recorded(sources, mailboxes, state);
  swim::core::RunLifecycle lifecycle{15ms};

  const auto started = std::chrono::steady_clock::now();
  const auto result = swim::core::run_decode_only(
      sources, mailboxes, 1, lifecycle, {}, 1ms);
  const auto elapsed = std::chrono::steady_clock::now() - started;

  CHECK_EQ(result, swim::core::DecodeOnlyExit::deadline_reached);
  CHECK(lifecycle.active());
  CHECK(elapsed >= 10ms);
  CHECK(elapsed < 500ms);
}

TEST_CASE(decode_only_exits_early_when_every_active_source_fails) {
  CountingBackend backend;
  swim::core::AppConfig config;
  auto sources = swim::core::make_sources(backend, config, 2);
  backend.views[0]->failure = true;
  backend.views[1]->failure = true;
  std::array<swim::core::LatestFrameMailbox, 6> mailboxes;
  swim::core::RunLifecycle lifecycle{10s};

  const auto started = std::chrono::steady_clock::now();
  const auto result = swim::core::run_decode_only(
      sources, mailboxes, 2, lifecycle, {}, 1ms);

  CHECK_EQ(result,
           swim::core::DecodeOnlyExit::all_sources_failed_before_active);
  CHECK(!lifecycle.active());
  CHECK(lifecycle.stop_requested());
  CHECK(std::chrono::steady_clock::now() - started < 500ms);
}
