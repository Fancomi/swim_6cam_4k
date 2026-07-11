#include "test_support.hpp"

#include <swim/core/benchmark_stage.hpp>

#include <array>
#include <cstdint>

namespace {

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
