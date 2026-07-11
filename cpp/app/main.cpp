#include <swim/core/asset.hpp>
#include <swim/core/backend.hpp>
#include <swim/core/benchmark_stage.hpp>
#include <swim/core/config.hpp>
#include <swim/core/metrics.hpp>
#include <swim/core/render_coordinator.hpp>
#include <swim/core/runtime_validation.hpp>
#include <swim/core/run_lifecycle.hpp>
#include <swim/core/runtime_start.hpp>

#if defined(SWIM_HAS_METAL_BACKEND)
#include <swim/metal/metal_backend.hpp>
#endif

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>
#include <thread>

namespace {

volatile std::sig_atomic_t signal_requested = 0;

extern "C" void request_shutdown_from_signal(int) {
  signal_requested = 1;
}

struct CommandLine {
  std::filesystem::path config_path;
  std::vector<std::string_view> overrides;
};

CommandLine parse_command_line(int argc, char* argv[]) {
  std::optional<std::filesystem::path> config_path;
  std::vector<std::string_view> overrides;
  overrides.reserve(static_cast<std::size_t>(argc > 1 ? argc - 1 : 0));

  for (int index = 1; index < argc; ++index) {
    const std::string_view argument{argv[index]};
    if (argument != "--config") {
      overrides.push_back(argument);
      continue;
    }
    if (config_path.has_value()) {
      throw std::runtime_error("duplicate command-line option '--config'");
    }
    if (index + 1 >= argc ||
        std::string_view{argv[index + 1]}.starts_with("--")) {
      throw std::runtime_error("--config requires PATH");
    }
    config_path = std::filesystem::path{argv[++index]};
  }

  if (!config_path.has_value()) {
    throw std::runtime_error("missing required --config PATH");
  }
  return CommandLine{std::move(*config_path), std::move(overrides)};
}

void require_regular_file(const std::filesystem::path& path,
                          std::string_view label) {
  std::error_code error;
  const auto is_file = std::filesystem::is_regular_file(path, error);
  if (!is_file || error) {
    throw std::runtime_error(std::string(label) + " does not exist: " +
                             path.string());
  }
}

swim::core::RuntimeAsset validate_inputs(
    const swim::core::AppConfig& config,
    const swim::core::BenchmarkGraph& graph) {
  require_regular_file(config.asset_path, "asset");
  for (std::uint32_t camera = 0; camera < graph.active_sources; ++camera) {
    const auto& source = config.sources[camera];
    require_regular_file(source.path, "source " + source.camera_id);
  }
  auto asset = swim::core::load_asset(config.asset_path);
  swim::core::validate_runtime_compatibility(config, asset, graph.encode);
  return asset;
}

void print_validation(const swim::core::AppConfig& config,
                      const swim::core::RuntimeAsset& asset) {
  std::cout << "configuration valid\n";
  std::cout << "backend=" << config.backend << '\n';
  std::cout << "camera_order=";
  for (std::size_t index = 0; index < config.sources.size(); ++index) {
    if (index != 0) {
      std::cout << ',';
    }
    std::cout << config.sources[index].camera_id;
  }
  std::cout << '\n';
  for (const auto& source : config.sources) {
    std::cout << "source." << source.camera_id << '=' << source.path.string()
              << '\n';
  }
  std::cout << "asset=" << config.asset_path.string() << '\n';
  std::cout << "dimensions=" << asset.logical_width << 'x'
            << asset.logical_height << " -> " << asset.encoded_width << 'x'
            << asset.encoded_height << '\n';
}

void write_final_metrics(const swim::core::AppConfig& config,
                         const swim::core::BenchmarkGraph& graph,
                         const swim::core::MetricsSnapshot& metrics,
                         std::chrono::steady_clock::duration elapsed,
                         std::size_t healthy_sources,
                         std::uint32_t output_width,
                         std::uint32_t output_height) {
  static_cast<void>(elapsed);
  const auto completion_interval_ns =
      metrics.render_completion_interval_ns();
  const auto render_fps = metrics.render_completion_fps();
  const auto encode_fps = metrics.encode_completion_fps();
  std::ostringstream line;
  line << std::fixed << std::setprecision(3)
       << "{\"final\":true"
       << ",\"requested_stage\":\""
       << swim::core::benchmark_stage_name(config.stage) << '\"'
       << ",\"requested_pacing\":\""
       << swim::core::pacing_name(config.mode) << '\"'
       << ",\"requested_stream_count\":" << config.stream_count
       << ",\"requested_preview\":"
       << (config.preview ? "true" : "false")
       << ",\"requested_encode\":"
       << (config.encode ? "true" : "false")
       << ",\"resolved_active_sources\":" << graph.active_sources
       << ",\"resolved_create_renderer\":"
       << (graph.create_renderer ? "true" : "false")
       << ",\"resolved_synthetic_inputs\":"
       << (graph.synthetic_inputs ? "true" : "false")
       << ",\"resolved_preview\":"
       << (graph.preview ? "true" : "false")
       << ",\"resolved_encode\":"
       << (graph.encode ? "true" : "false")
       << ",\"received\":" << metrics.received
       << ",\"decoded\":" << metrics.decoded
       << ",\"published\":" << metrics.published
       << ",\"overwritten\":" << metrics.overwritten
       << ",\"reused\":" << metrics.reused
       << ",\"render_submissions\":" << metrics.render_submissions
       << ",\"render_completions\":" << metrics.render_completions
       << ",\"render_drops\":" << metrics.render_drops
       << ",\"render_active_ns\":" << metrics.render_active_ns
       << ",\"render_first_submit_ns\":" << metrics.render_first_submit_ns
       << ",\"render_last_completion_ns\":"
       << metrics.render_last_completion_ns
       << ",\"render_completion_interval_ns\":" << completion_interval_ns
       << ",\"render_fps\":" << render_fps
       << ",\"render_inflight_capacity\":"
       << metrics.render_inflight_capacity
       << ",\"render_inflight_high_water\":"
       << metrics.render_inflight_high_water
       << ",\"render_inflight_pool_misses\":"
       << metrics.render_inflight_pool_misses
       << ",\"render_output_capacity\":" << metrics.render_output_capacity
       << ",\"render_output_high_water\":"
       << metrics.render_output_high_water
       << ",\"render_output_pool_misses\":"
       << metrics.render_output_pool_misses
       << ",\"frame_age_ms_p99\":[";
  for (std::size_t camera = 0; camera < metrics.frame_age_ms_p99.size();
       ++camera) {
    if (camera != 0) {
      line << ',';
    }
    line << metrics.frame_age_ms_p99[camera];
  }
  line << ']'
       << ",\"preview_drops\":" << metrics.preview_drops
       << ",\"preview_presents\":" << metrics.preview_presents
       << ",\"encode_submissions\":" << metrics.encode_submissions
       << ",\"encode_completions\":" << metrics.encode_completions
       << ",\"encode_bytes\":" << metrics.encode_bytes
       << ",\"encode_drops\":" << metrics.encode_drops
       << ",\"encode_rejected_frames\":" << metrics.encode_rejected_frames
       << ",\"encode_callback_errors\":" << metrics.encode_callback_errors
       << ",\"encode_first_submit_ns\":" << metrics.encode_first_submit_ns
       << ",\"encode_last_completion_ns\":"
       << metrics.encode_last_completion_ns
       << ",\"encode_fps\":" << encode_fps
       << ",\"encode_input_capacity\":" << metrics.encode_input_capacity
       << ",\"encode_input_in_use\":" << metrics.encode_input_in_use
       << ",\"encode_input_high_water\":"
       << metrics.encode_input_high_water
       << ",\"encode_input_pool_misses\":"
       << metrics.encode_input_pool_misses
       << ",\"encode_using_hardware\":"
       << (metrics.encode_using_hardware ? "true" : "false")
       << ",\"encode_drain_timeouts\":" << metrics.encode_drain_timeouts
       << ",\"encode_codec\":\"hevc\""
       << ",\"pool_exhaustion\":" << metrics.pool_exhaustion
       << ",\"decoded_pixel_host_copies\":"
       << metrics.decoded_pixel_host_copies
       << ",\"native_texture_wrappers\":"
       << metrics.native_texture_wrappers
       << ",\"native_command_buffers\":"
       << metrics.native_command_buffers
       << ",\"native_decode_tickets\":"
       << metrics.native_decode_tickets
       << ",\"native_callback_wrappers\":"
       << metrics.native_callback_wrappers
       << ",\"sources_healthy\":" << healthy_sources
       << ",\"output_width\":" << output_width
       << ",\"output_height\":" << output_height << '}'
       << '\n';
  std::cout << line.str();

  if (!config.metrics_path.empty()) {
    const auto parent = config.metrics_path.parent_path();
    if (!parent.empty()) {
      std::filesystem::create_directories(parent);
    }
    std::ofstream output(config.metrics_path, std::ios::app);
    if (!output) {
      throw std::runtime_error("cannot open metrics output: " +
                               config.metrics_path.string());
    }
    output << line.str();
  }
}

// Installed before any backend component exists. It owns no native pointers,
// so setup failures at every phase can still emit exactly one final line.
class FinalMetricsGuard final {
 public:
  FinalMetricsGuard(const swim::core::AppConfig& config,
                    const swim::core::BenchmarkGraph& graph,
                    const swim::core::RuntimeAsset& asset,
                    swim::core::RuntimeCounters& metrics,
                    std::chrono::steady_clock::time_point started_at) noexcept
      : config_(config),
        graph_(graph),
        asset_(asset),
        metrics_(metrics),
        started_at_(started_at) {}

  ~FinalMetricsGuard() noexcept {
    if (emitted_) {
      return;
    }
    try {
      emit(healthy_sources_);
    } catch (const std::exception& error) {
      std::cerr << "final metrics error: " << error.what() << '\n';
    } catch (...) {
      std::cerr << "final metrics error: unknown failure\n";
    }
  }

  void emit(std::size_t healthy_sources) {
    if (emitted_) {
      return;
    }
    healthy_sources_ = healthy_sources;
    const auto elapsed = std::chrono::steady_clock::now() - started_at_;
    const auto snapshot = metrics_.snapshot_and_reset();
    emitted_ = true;
    write_final_metrics(config_, graph_, snapshot, elapsed, healthy_sources_,
                        asset_.encoded_width, asset_.encoded_height);
  }

 private:
  const swim::core::AppConfig& config_;
  const swim::core::BenchmarkGraph& graph_;
  const swim::core::RuntimeAsset& asset_;
  swim::core::RuntimeCounters& metrics_;
  std::chrono::steady_clock::time_point started_at_;
  std::size_t healthy_sources_{};
  bool emitted_{};
};

class RuntimeFinalizer final {
 public:
  RuntimeFinalizer(
      const swim::core::AppConfig& config,
      swim::core::IBackend& backend,
      swim::core::IRenderer* renderer,
      swim::core::SourceArray& sources,
      swim::core::RunLifecycle& lifecycle,
      swim::core::RuntimeStartState& start_state,
      FinalMetricsGuard& final_metrics) noexcept
      : config_(config),
        backend_(backend),
        renderer_(renderer),
        sources_(sources),
        lifecycle_(lifecycle),
        start_state_(start_state),
        final_metrics_(final_metrics) {}

  ~RuntimeFinalizer() noexcept {
    if (finalized_) {
      return;
    }
    try {
      finalize();
    } catch (const std::exception& error) {
      std::cerr << "runtime cleanup error: " << error.what() << '\n';
    } catch (...) {
      std::cerr << "runtime cleanup error: unknown failure\n";
    }
  }

  void finalize() {
    if (finalized_) {
      return;
    }
    backend_.stop_main_loop();
    lifecycle_.request_stop();
    render_thread.request_stop();
    if (render_thread.joinable()) {
      render_thread.join();
    }
    signal_monitor.request_stop();
    if (signal_monitor.joinable()) {
      signal_monitor.join();
    }
    swim::core::stop_sources(sources_);

    std::exception_ptr cleanup_error = render_error;
    if (renderer_ != nullptr) {
      try {
        renderer_->drain();
      } catch (...) {
        if (!cleanup_error) {
          cleanup_error = std::current_exception();
        }
      }
    }
    if (!cleanup_error && renderer_ != nullptr &&
        renderer_->has_fatal_error()) {
      auto message = renderer_->last_error();
      if (message.empty()) {
        message = "renderer reported a fatal native error during drain";
      }
      cleanup_error =
          std::make_exception_ptr(std::runtime_error(std::move(message)));
    }

    std::array<bool, 6> failed{};
    bool started_source_failed = false;
    for (std::size_t camera = 0; camera < sources_.size(); ++camera) {
      if (!sources_[camera]) {
        continue;
      }
      failed[camera] = sources_[camera]->failed();
      if (start_state_.started(camera) && failed[camera]) {
        started_source_failed = true;
        std::cerr << "source " << config_.sources[camera].camera_id
                  << " failed: " << sources_[camera]->last_error() << '\n';
      }
    }
    if (started_source_failed && !cleanup_error) {
      cleanup_error = std::make_exception_ptr(
          std::runtime_error("one or more source lanes failed"));
    }
    finalized_ = true;
    final_metrics_.emit(start_state_.healthy_count(failed));
    if (cleanup_error) {
      std::rethrow_exception(cleanup_error);
    }
  }

  std::jthread render_thread;
  std::jthread signal_monitor;
  std::exception_ptr render_error;

 private:
  const swim::core::AppConfig& config_;
  swim::core::IBackend& backend_;
  swim::core::IRenderer* renderer_;
  swim::core::SourceArray& sources_;
  swim::core::RunLifecycle& lifecycle_;
  swim::core::RuntimeStartState& start_state_;
  FinalMetricsGuard& final_metrics_;
  bool finalized_{};
};

int run_runtime(const swim::core::AppConfig& config,
                const swim::core::RuntimeAsset& asset,
                const swim::core::BenchmarkGraph& graph) {
  swim::core::RuntimeCounters metrics;
  swim::core::RunLifecycle lifecycle{config.duration};
  swim::core::RuntimeStartState start_state;
  const auto started_at = std::chrono::steady_clock::now();
  FinalMetricsGuard final_metrics{config, graph, asset, metrics, started_at};
  try {
#if defined(SWIM_HAS_METAL_BACKEND)
    swim::metal::register_metal_backend();
#endif
    auto backend =
        swim::core::BackendRegistry::instance().create(config.backend);
    backend->bind_metrics(metrics);
    backend->bind_lifecycle(lifecycle);
    auto renderer = backend->make_renderer(asset, config, graph);
    if (graph.create_renderer != static_cast<bool>(renderer)) {
      throw std::runtime_error(
          "backend renderer creation does not match resolved graph");
    }
    // Mailboxes precede publishers so reverse destruction can never destroy a
    // mailbox while a source object still owns its address.
    swim::core::MailboxArray mailboxes;
    auto sources =
        swim::core::make_sources(*backend, config, graph.active_sources);
    RuntimeFinalizer finalizer{config, *backend, renderer.get(), sources,
                               lifecycle, start_state, final_metrics};

    swim::core::start_sources_recorded(sources, mailboxes, start_state);

    finalizer.render_thread = std::jthread([&](std::stop_token token) {
      try {
        if (renderer != nullptr) {
          swim::core::RenderCoordinator coordinator{
              mailboxes, *renderer, config, graph, metrics, lifecycle};
          coordinator.run(token);
        } else {
          static_cast<void>(swim::core::run_decode_only(
              sources, mailboxes, graph.active_sources, lifecycle, token));
        }
      } catch (...) {
        finalizer.render_error = std::current_exception();
      }
      backend->stop_main_loop();
    });

    finalizer.signal_monitor = std::jthread([&](std::stop_token token) {
      while (!token.stop_requested()) {
        if (signal_requested != 0) {
          lifecycle.request_stop();
          finalizer.render_thread.request_stop();
          backend->stop_main_loop();
          return;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds{10});
      }
    });

    backend->run_main_loop(finalizer.render_thread.get_stop_token());
    finalizer.finalize();
  } catch (...) {
    const auto primary_error = std::current_exception();
    try {
      final_metrics.emit(start_state.started_count());
    } catch (const std::exception& cleanup_error) {
      std::cerr << "runtime cleanup error: " << cleanup_error.what() << '\n';
    } catch (...) {
      std::cerr << "runtime cleanup error: unknown failure\n";
    }
    std::rethrow_exception(primary_error);
  }
  return 0;
}

int run(int argc, char* argv[]) {
  const auto command_line = parse_command_line(argc, argv);
  auto config = swim::core::load_config(command_line.config_path);
  config = swim::core::apply_cli_overrides(std::move(config),
                                           command_line.overrides);
  static_cast<void>(swim::core::resolve_benchmark_manifest(config));
  const auto graph = swim::core::resolve_benchmark_graph(config);
  auto asset = validate_inputs(config, graph);

  if (config.validate_only) {
    print_validation(config, asset);
    return 0;
  }
  return run_runtime(config, asset, graph);
}

}  // namespace

int main(int argc, char* argv[]) {
  std::signal(SIGINT, request_shutdown_from_signal);
  std::signal(SIGTERM, request_shutdown_from_signal);
  try {
    return run(argc, argv);
  } catch (const std::exception& error) {
    std::cerr << "error: " << error.what() << '\n';
    return 1;
  }
}
