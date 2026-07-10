#include <swim/core/config.hpp>

#include <array>
#include <charconv>
#include <fstream>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <utility>

namespace swim::core {
namespace {

constexpr std::array<std::string_view, 6> kCameraIds{
    "cam3", "cam2", "cam1", "cam4", "cam5", "cam6"};

std::string_view trim_ascii(std::string_view value) {
  constexpr std::string_view whitespace{" \t\r\n\f\v"};
  const auto first = value.find_first_not_of(whitespace);
  if (first == std::string_view::npos) {
    return {};
  }
  const auto last = value.find_last_not_of(whitespace);
  return value.substr(first, last - first + 1);
}

[[noreturn]] void config_error(const std::filesystem::path& path,
                               std::size_t line,
                               const std::string& message) {
  throw std::runtime_error(path.string() + ":" + std::to_string(line) +
                           ": " + message);
}

std::uint32_t parse_unsigned(std::string_view value,
                             std::string_view option) {
  std::uint32_t parsed{};
  const auto result =
      std::from_chars(value.data(), value.data() + value.size(), parsed);
  if (value.empty() || result.ec != std::errc{} ||
      result.ptr != value.data() + value.size()) {
    throw std::runtime_error(std::string(option) +
                             " must be an unsigned integer");
  }
  return parsed;
}

std::uint32_t parse_config_unsigned(const std::filesystem::path& path,
                                    std::size_t line, std::string_view key,
                                    std::string_view value) {
  try {
    return parse_unsigned(value, key);
  } catch (const std::runtime_error& error) {
    config_error(path, line, error.what());
  }
}

bool parse_boolean(std::string_view value, std::string_view option) {
  if (value == "true") {
    return true;
  }
  if (value == "false") {
    return false;
  }
  throw std::runtime_error(std::string(option) + " must be true or false");
}

bool parse_config_boolean(const std::filesystem::path& path, std::size_t line,
                          std::string_view key, std::string_view value) {
  try {
    return parse_boolean(value, key);
  } catch (const std::runtime_error& error) {
    config_error(path, line, error.what());
  }
}

RunMode parse_mode(std::string_view value, std::string_view option) {
  if (value == "realtime") {
    return RunMode::realtime;
  }
  if (value == "benchmark") {
    return RunMode::benchmark;
  }
  throw std::runtime_error("invalid " + std::string(option) + " value '" +
                           std::string(value) + "'");
}

BenchmarkStage parse_stage(std::string_view value, std::string_view option) {
  if (value == "full") {
    return BenchmarkStage::full;
  }
  if (value == "decode-only") {
    return BenchmarkStage::decode_only;
  }
  if (value == "render-only") {
    return BenchmarkStage::render_only;
  }
  if (value == "decode-render") {
    return BenchmarkStage::decode_render;
  }
  if (value == "decode-render-preview") {
    return BenchmarkStage::decode_render_preview;
  }
  if (value == "decode-render-encode") {
    return BenchmarkStage::decode_render_encode;
  }
  throw std::runtime_error("invalid " + std::string(option) + " value '" +
                           std::string(value) + "'");
}

template <class Parser>
auto parse_config_enum(const std::filesystem::path& path, std::size_t line,
                       std::string_view key, std::string_view value,
                       Parser parser) {
  try {
    return parser(value, key);
  } catch (const std::runtime_error& error) {
    config_error(path, line, error.what());
  }
}

std::size_t source_index(std::string_view key) {
  for (std::size_t index = 0; index < kCameraIds.size(); ++index) {
    if (key == "source." + std::string(kCameraIds[index])) {
      return index;
    }
  }
  return kCameraIds.size();
}

std::string_view option_name(std::string_view argument) {
  const auto equals = argument.find('=');
  return argument.substr(0, equals);
}

std::string_view option_value(std::string_view argument) {
  const auto equals = argument.find('=');
  if (equals == std::string_view::npos) {
    return {};
  }
  return argument.substr(equals + 1);
}

std::filesystem::path parse_cli_path(std::string_view value,
                                     std::string_view option) {
  if (value.empty()) {
    throw std::runtime_error(std::string(option) + " requires PATH");
  }
  return std::filesystem::path{value};
}

}  // namespace

AppConfig load_config(const std::filesystem::path& path) {
  std::ifstream input(path);
  if (!input) {
    config_error(path, 0, "cannot open config");
  }

  AppConfig config;
  std::unordered_set<std::string> seen_keys;
  std::array<bool, kCameraIds.size()> seen_sources{};
  std::string storage;
  std::size_t line_number = 0;
  while (std::getline(input, storage)) {
    ++line_number;
    const auto line = trim_ascii(storage);
    if (line.empty() || line.front() == '#') {
      continue;
    }

    const auto equals = line.find('=');
    if (equals == std::string_view::npos) {
      config_error(path, line_number, "expected key=value");
    }
    const auto key = trim_ascii(line.substr(0, equals));
    const auto value = trim_ascii(line.substr(equals + 1));
    if (key.empty()) {
      config_error(path, line_number, "empty key");
    }
    if (!seen_keys.emplace(key).second) {
      config_error(path, line_number,
                   "duplicate key '" + std::string(key) + "'");
    }

    const auto camera_index = source_index(key);
    if (camera_index < kCameraIds.size()) {
      config.sources[camera_index].path = std::filesystem::path{value};
      seen_sources[camera_index] = true;
    } else if (key == "backend") {
      config.backend = value;
    } else if (key == "mode") {
      config.mode = parse_config_enum(path, line_number, key, value, parse_mode);
    } else if (key == "stage") {
      config.stage =
          parse_config_enum(path, line_number, key, value, parse_stage);
    } else if (key == "asset") {
      config.asset_path = std::filesystem::path{value};
    } else if (key == "fps_num") {
      config.fps_num = parse_config_unsigned(path, line_number, key, value);
    } else if (key == "fps_den") {
      config.fps_den = parse_config_unsigned(path, line_number, key, value);
    } else if (key == "preview") {
      config.preview = parse_config_boolean(path, line_number, key, value);
    } else if (key == "encode") {
      config.encode = parse_config_boolean(path, line_number, key, value);
    } else if (key == "diagnostic_replacement") {
      config.diagnostic_replacement =
          parse_config_boolean(path, line_number, key, value);
    } else if (key == "encode_path") {
      config.encode_path = std::filesystem::path{value};
    } else if (key == "stale_ms") {
      config.stale_after = std::chrono::milliseconds{
          parse_config_unsigned(path, line_number, key, value)};
    } else if (key == "replace_ms") {
      config.replace_after = std::chrono::milliseconds{
          parse_config_unsigned(path, line_number, key, value)};
    } else if (key == "decode_surface_pool") {
      config.decode_surface_pool =
          parse_config_unsigned(path, line_number, key, value);
    } else if (key == "decode_ticket_pool") {
      config.decode_ticket_pool =
          parse_config_unsigned(path, line_number, key, value);
    } else if (key == "render_inflight") {
      config.render_inflight =
          parse_config_unsigned(path, line_number, key, value);
    } else if (key == "output_pool") {
      config.output_pool =
          parse_config_unsigned(path, line_number, key, value);
    } else if (key == "duration_seconds") {
      config.duration = std::chrono::seconds{
          parse_config_unsigned(path, line_number, key, value)};
    } else if (key == "metrics") {
      config.metrics_path = std::filesystem::path{value};
    } else {
      config_error(path, line_number,
                   "unknown key '" + std::string(key) + "'");
    }
  }

  for (const auto seen : seen_sources) {
    if (!seen) {
      config_error(path, line_number + 1,
                   "sources must be exactly cam3,cam2,cam1,cam4,cam5,cam6");
    }
  }
  return config;
}

AppConfig apply_cli_overrides(
    AppConfig config, std::span<const std::string_view> arguments) {
  std::unordered_set<std::string> seen_options;
  for (const auto argument : arguments) {
    const auto name = option_name(argument);
    if (!seen_options.emplace(name).second) {
      throw std::runtime_error("duplicate command-line option '" +
                               std::string(name) + "'");
    }

    if (argument == "--validate-only") {
      config.validate_only = true;
      continue;
    }

    const auto value = option_value(argument);
    if (name == "--preview") {
      config.preview = parse_boolean(value, name);
    } else if (name == "--encode") {
      config.encode = parse_boolean(value, name);
    } else if (name == "--diagnostic-replacement") {
      config.diagnostic_replacement = parse_boolean(value, name);
    } else if (name == "--encode-path") {
      config.encode_path = parse_cli_path(value, name);
    } else if (name == "--encode-sink") {
      if (value == "file") {
        config.encode_sink = EncodeSink::file;
      } else if (value == "null") {
        config.encode_sink = EncodeSink::null_sink;
      } else {
        throw std::runtime_error("--encode-sink must be file or null");
      }
    } else if (name == "--duration-seconds") {
      config.duration =
          std::chrono::seconds{parse_unsigned(value, "--duration-seconds")};
    } else if (name == "--mode") {
      config.mode = parse_mode(value, "--mode");
    } else if (name == "--stage") {
      config.stage = parse_stage(value, "--stage");
    } else if (name == "--stream-count") {
      const auto count = parse_unsigned(value, "--stream-count");
      if (count != 1 && count != 2 && count != 4 && count != 6) {
        throw std::runtime_error("--stream-count must be one of 1,2,4,6");
      }
      config.stream_count = count;
    } else if (name == "--metrics") {
      config.metrics_path = parse_cli_path(value, name);
    } else {
      throw std::runtime_error("unknown command-line option '" +
                               std::string(argument) + "'");
    }
  }
  return config;
}

}  // namespace swim::core
