#include <swim/core/asset.hpp>
#include <swim/core/backend.hpp>
#include <swim/core/benchmark_reporter.hpp>
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

#if defined(SWIM_HAS_D3D11_BACKEND)
#include <swim/d3d11/d3d11_backend.hpp>
#endif

#if defined(SWIM_HAS_CUDAGL_BACKEND)
#include <swim/cudagl/cudagl_backend.hpp>
#endif

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <timeapi.h>
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

class RuntimeFinalizer final {
 public:
  RuntimeFinalizer(
      const swim::core::AppConfig& config,
      swim::core::IBackend& backend,
      swim::core::IRenderer* renderer,
      swim::core::SourceArray& sources,
      swim::core::RunLifecycle& lifecycle,
      swim::core::RuntimeStartState& start_state,
      swim::core::BenchmarkReporter& reporter) noexcept
      : config_(config),
        backend_(backend),
        renderer_(renderer),
        sources_(sources),
        lifecycle_(lifecycle),
        start_state_(start_state),
        reporter_(reporter) {}

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
    stats_monitor.request_stop();
    if (stats_monitor.joinable()) {
      stats_monitor.join();
    }
    swim::core::stop_sources(sources_);
    std::exception_ptr cleanup_error = render_error;
    try {
      reporter_.stop_intervals();
    } catch (...) {
      if (!cleanup_error) {
        cleanup_error = std::current_exception();
      }
    }
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
    try {
      reporter_.write_final(start_state_.healthy_count(failed));
    } catch (...) {
      if (!cleanup_error) {
        cleanup_error = std::current_exception();
      }
    }
    reporter_.unbind_backend();
    finalized_ = true;
    if (cleanup_error) {
      std::rethrow_exception(cleanup_error);
    }
  }

  std::jthread render_thread;
  std::jthread signal_monitor;
  std::jthread stats_monitor;
  std::exception_ptr render_error;

 private:
  const swim::core::AppConfig& config_;
  swim::core::IBackend& backend_;
  swim::core::IRenderer* renderer_;
  swim::core::SourceArray& sources_;
  swim::core::RunLifecycle& lifecycle_;
  swim::core::RuntimeStartState& start_state_;
  swim::core::BenchmarkReporter& reporter_;
  bool finalized_{};
};

int run_runtime(const swim::core::AppConfig& config,
                const swim::core::RuntimeAsset& asset,
                const swim::core::BenchmarkGraph& graph,
                std::optional<swim::core::BenchmarkManifest> manifest) {
  swim::core::RuntimeCounters metrics;
  swim::core::RunLifecycle lifecycle{config.duration};
  swim::core::RuntimeStartState start_state;
  swim::core::BenchmarkReporter reporter{
      {config, graph, std::move(manifest), asset.encoded_width,
      asset.encoded_height, {}},
      metrics};
  std::unique_ptr<swim::core::IBackend> backend;
  try {
#if defined(SWIM_HAS_METAL_BACKEND)
    swim::metal::register_metal_backend();
#endif
#if defined(SWIM_HAS_D3D11_BACKEND)
    swim::d3d11::register_d3d11_backend();
#endif
#if defined(SWIM_HAS_CUDAGL_BACKEND)
    swim::cudagl::register_cudagl_backend();
#endif
    backend = swim::core::BackendRegistry::instance().create(config.backend);
    reporter.bind_backend(*backend);
    reporter.start();
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
                               lifecycle, start_state, reporter};

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

    // Live efficiency line: once a second, print render/decode/preview FPS to
    // stderr on a single rewritten line so the operator sees realtime
    // throughput without parsing the metrics JSONL. Suppressed for
    // validate-only and benchmark manifests (those parse stdout/stderr).
    if (renderer != nullptr && config.mode == swim::core::RunMode::realtime) {
      finalizer.stats_monitor = std::jthread([&](std::stop_token token) {
        using Clock = std::chrono::steady_clock;
        // MetricsSnapshot has const members and is not assignable, so track the
        // few cumulative counters we need as plain scalars across ticks.
        auto previous_at = Clock::now();
        std::uint64_t prev_render = metrics.render_completions.load(
            std::memory_order_relaxed);
        std::uint64_t prev_decode = metrics.decoded.load(
            std::memory_order_relaxed);
        std::uint64_t prev_preview = metrics.preview_presents.load(
            std::memory_order_relaxed);
        std::uint64_t prev_drops = metrics.render_drops.load(
            std::memory_order_relaxed);
        while (!token.stop_requested()) {
          std::this_thread::sleep_for(std::chrono::milliseconds{500});
          if (token.stop_requested()) {
            break;
          }
          const auto now = Clock::now();
          const auto elapsed =
              std::chrono::duration<double>(now - previous_at).count();
          if (elapsed < 0.99) {
            continue;
          }
          const auto cur_render =
              metrics.render_completions.load(std::memory_order_relaxed);
          const auto cur_decode =
              metrics.decoded.load(std::memory_order_relaxed);
          const auto cur_preview =
              metrics.preview_presents.load(std::memory_order_relaxed);
          const auto cur_drops =
              metrics.render_drops.load(std::memory_order_relaxed);
          const auto rate = [elapsed](std::uint64_t a, std::uint64_t b) {
            return elapsed > 0.0 ? static_cast<double>(a - b) / elapsed : 0.0;
          };
          std::fprintf(stderr,
                       "\r[%s] render %5.1f fps | decode %5.1f fps/cam | "
                       "preview %5.1f fps | drops %4.0f/s   ",
                       config.backend.c_str(), rate(cur_render, prev_render),
                       rate(cur_decode, prev_decode) / 6.0,
                       rate(cur_preview, prev_preview),
                       rate(cur_drops, prev_drops));
          std::fflush(stderr);
          prev_render = cur_render;
          prev_decode = cur_decode;
          prev_preview = cur_preview;
          prev_drops = cur_drops;
          previous_at = now;
        }
        std::fprintf(stderr, "\n");
        std::fflush(stderr);
      });
    }

    backend->run_main_loop(finalizer.render_thread.get_stop_token());
    finalizer.finalize();
  } catch (...) {
    const auto primary_error = std::current_exception();
    try {
      reporter.stop_intervals();
    } catch (const std::exception& cleanup_error) {
      std::cerr << "runtime cleanup error: " << cleanup_error.what() << '\n';
    } catch (...) {
      std::cerr << "runtime cleanup error: unknown failure\n";
    }
    try {
      reporter.write_final(start_state.started_count());
    } catch (const std::exception& cleanup_error) {
      std::cerr << "runtime cleanup error: " << cleanup_error.what() << '\n';
    } catch (...) {
      std::cerr << "runtime cleanup error: unknown failure\n";
    }
    reporter.unbind_backend();
    std::rethrow_exception(primary_error);
  }
  return 0;
}

int run(int argc, char* argv[]) {
  const auto command_line = parse_command_line(argc, argv);
  auto config = swim::core::load_config(command_line.config_path);
  config = swim::core::apply_cli_overrides(std::move(config),
                                           command_line.overrides);
  auto manifest = swim::core::resolve_benchmark_manifest(config);
  const auto graph = swim::core::resolve_benchmark_graph(config);
  auto asset = validate_inputs(config, graph);

  if (config.validate_only) {
    print_validation(config, asset);
    return 0;
  }
  return run_runtime(config, asset, graph, std::move(manifest));
}

}  // namespace

int main(int argc, char* argv[]) {
  std::signal(SIGINT, request_shutdown_from_signal);
  std::signal(SIGTERM, request_shutdown_from_signal);
#if defined(_WIN32)
  // The render coordinator paces on condition-variable timed waits. The default
  // Windows timer granularity (~15.6 ms) would quantize a 33 ms cadence badly;
  // request 1 ms resolution for the run and restore it on exit.
  timeBeginPeriod(1);
  struct TimePeriodGuard {
    ~TimePeriodGuard() { timeEndPeriod(1); }
  } time_period_guard;
#endif
  try {
    return run(argc, argv);
  } catch (const std::exception& error) {
    std::cerr << "error: " << error.what() << '\n';
    return 1;
  }
}
