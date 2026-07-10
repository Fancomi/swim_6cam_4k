#include <swim/core/asset.hpp>
#include <swim/core/backend.hpp>
#include <swim/core/config.hpp>

#include <array>
#include <cstddef>
#include <filesystem>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <system_error>
#include <utility>
#include <vector>

namespace {

constexpr std::array<std::string_view, 6> kCameraIds{
    "cam3", "cam2", "cam1", "cam4", "cam5", "cam6"};

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

void validate_camera_order(const swim::core::AppConfig& config,
                           const swim::core::RuntimeAsset& asset) {
  if (asset.cameras.size() != kCameraIds.size()) {
    throw std::runtime_error("asset must contain exactly six cameras");
  }
  for (std::size_t index = 0; index < kCameraIds.size(); ++index) {
    if (config.sources[index].camera_id != kCameraIds[index] ||
        asset.cameras[index].camera_id != kCameraIds[index]) {
      throw std::runtime_error(
          "camera order must be cam3,cam2,cam1,cam4,cam5,cam6");
    }
  }
}

swim::core::RuntimeAsset validate_inputs(const swim::core::AppConfig& config) {
  require_regular_file(config.asset_path, "asset");
  for (const auto& source : config.sources) {
    require_regular_file(source.path, "source " + source.camera_id);
  }
  auto asset = swim::core::load_asset(config.asset_path);
  validate_camera_order(config, asset);
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

  auto backend = swim::core::BackendRegistry::instance().create(config.backend);
  static_cast<void>(backend);
  return 0;
}

}  // namespace

int main(int argc, char* argv[]) {
  try {
    return run(argc, argv);
  } catch (const std::exception& error) {
    std::cerr << "error: " << error.what() << '\n';
    return 1;
  }
}
