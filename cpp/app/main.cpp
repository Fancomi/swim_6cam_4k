#include <swim/core/asset.hpp>
#include <swim/core/backend.hpp>
#include <swim/core/config.hpp>
#include <swim/core/metrics.hpp>
#include <swim/core/render_coordinator.hpp>
#include <swim/core/runtime_validation.hpp>

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

swim::core::RuntimeAsset validate_inputs(const swim::core::AppConfig& config) {
  require_regular_file(config.asset_path, "asset");
  for (const auto& source : config.sources) {
    require_regular_file(source.path, "source " + source.camera_id);
  }
  auto asset = swim::core::load_asset(config.asset_path);
  swim::core::validate_runtime_compatibility(config, asset);
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
                         const swim::core::MetricsSnapshot& metrics,
                         std::chrono::steady_clock::duration elapsed,
                         std::size_t healthy_sources,
                         std::uint32_t output_width,
                         std::uint32_t output_height) {
  const auto seconds = metrics.render_active_ns == 0
                           ? std::chrono::duration<double>(elapsed).count()
                           : static_cast<double>(metrics.render_active_ns) /
                                 1'000'000'000.0;
  const auto render_fps = seconds > 0.0
                              ? static_cast<double>(metrics.render_submissions) /
                                    seconds
                              : 0.0;
  std::ostringstream line;
  line << std::fixed << std::setprecision(3)
       << "{\"final\":true"
       << ",\"received\":" << metrics.received
       << ",\"decoded\":" << metrics.decoded
       << ",\"published\":" << metrics.published
       << ",\"overwritten\":" << metrics.overwritten
       << ",\"reused\":" << metrics.reused
       << ",\"render_submissions\":" << metrics.render_submissions
       << ",\"render_drops\":" << metrics.render_drops
       << ",\"render_active_ns\":" << metrics.render_active_ns
       << ",\"render_fps\":" << render_fps
       << ",\"pool_exhaustion\":" << metrics.pool_exhaustion
       << ",\"decoded_pixel_host_copies\":"
       << metrics.decoded_pixel_host_copies
       << ",\"native_texture_wrappers\":"
       << metrics.native_texture_wrappers
       << ",\"native_command_buffers\":"
       << metrics.native_command_buffers
       << ",\"native_decode_tickets\":"
       << metrics.native_decode_tickets
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

std::array<std::unique_ptr<swim::core::ISource>, 6> make_six_sources(
    swim::core::IBackend& backend, const swim::core::AppConfig& config) {
  std::array<std::unique_ptr<swim::core::ISource>, 6> sources;
  for (std::uint32_t camera = 0; camera < sources.size(); ++camera) {
    sources[camera] = backend.make_source(config.sources[camera], camera);
  }
  return sources;
}

void start_sources(
    std::array<std::unique_ptr<swim::core::ISource>, 6>& sources,
    std::array<swim::core::LatestFrameMailbox, 6>& mailboxes) {
  std::size_t started = 0;
  try {
    for (; started < sources.size(); ++started) {
      sources[started]->start(mailboxes[started]);
    }
  } catch (...) {
    while (started > 0) {
      sources[--started]->stop();
    }
    throw;
  }
}

void stop_sources(
    std::array<std::unique_ptr<swim::core::ISource>, 6>& sources) noexcept {
  for (auto& source : sources) {
    source->stop();
  }
}

int run_runtime(const swim::core::AppConfig& config,
                const swim::core::RuntimeAsset& asset) {
#if defined(SWIM_HAS_METAL_BACKEND)
  swim::metal::register_metal_backend();
#endif
  swim::core::RuntimeCounters metrics;
  auto backend = swim::core::BackendRegistry::instance().create(config.backend);
  backend->bind_metrics(metrics);
  auto renderer = backend->make_renderer(asset, config);
  auto sources = make_six_sources(*backend, config);
  std::array<swim::core::LatestFrameMailbox, 6> mailboxes;
  start_sources(sources, mailboxes);

  const auto started_at = std::chrono::steady_clock::now();
  std::exception_ptr render_error;
  std::jthread render_thread([&](std::stop_token token) {
    try {
      swim::core::RenderCoordinator coordinator{mailboxes, *renderer, config,
                                                 metrics};
      coordinator.run(token);
    } catch (...) {
      render_error = std::current_exception();
    }
    backend->stop_main_loop();
  });

  std::jthread signal_monitor([&](std::stop_token token) {
    while (!token.stop_requested()) {
      if (signal_requested != 0) {
        render_thread.request_stop();
        backend->stop_main_loop();
        return;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds{10});
    }
  });

  backend->run_main_loop(render_thread.get_stop_token());
  render_thread.request_stop();
  render_thread.join();
  signal_monitor.request_stop();
  signal_monitor.join();
  stop_sources(sources);
  renderer->drain();
  if (!render_error && renderer->has_fatal_error()) {
    auto message = renderer->last_error();
    if (message.empty()) {
      message = "renderer reported a fatal native error during drain";
    }
    render_error = std::make_exception_ptr(std::runtime_error(message));
  }

  std::size_t healthy_sources = 0;
  for (std::size_t camera = 0; camera < sources.size(); ++camera) {
    if (!sources[camera]->failed()) {
      ++healthy_sources;
    } else {
      std::cerr << "source " << config.sources[camera].camera_id
                << " failed: " << sources[camera]->last_error() << '\n';
    }
  }
  const auto elapsed = std::chrono::steady_clock::now() - started_at;
  write_final_metrics(config, metrics.snapshot_and_reset(), elapsed,
                      healthy_sources, asset.encoded_width,
                      asset.encoded_height);
  if (render_error) {
    std::rethrow_exception(render_error);
  }
  return 0;
}

int run(int argc, char* argv[]) {
  const auto command_line = parse_command_line(argc, argv);
  auto config = swim::core::load_config(command_line.config_path);
  config = swim::core::apply_cli_overrides(std::move(config),
                                           command_line.overrides);
  auto asset = validate_inputs(config);

  if (config.validate_only) {
    print_validation(config, asset);
    return 0;
  }
  return run_runtime(config, asset);
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
