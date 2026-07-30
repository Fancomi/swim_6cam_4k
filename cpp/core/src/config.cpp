#include <swim/core/config.hpp>

#include <array>
#include <charconv>
#include <cstdlib>
#include <fstream>
#include <stdexcept>
#include <string>
#include <tuple>
#include <unordered_set>
#include <utility>
#include <vector>

namespace swim::core {
namespace {

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
  if (value.empty()) {
    throw std::runtime_error(std::string(option) +
                             " must be an unsigned integer");
  }
  std::uint32_t parsed{};
  const auto* const end = value.data() + value.size();
  const auto result = std::from_chars(value.data(), end, parsed);
  if (result.ec != std::errc{} || result.ptr != end) {
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

constexpr std::string_view kSourcePrefix{"source."};

// Returns the camera id in `source.<id>` keys, or empty when `key` is not one.
std::string_view source_camera_id(std::string_view key) {
  if (!key.starts_with(kSourcePrefix)) {
    return {};
  }
  return key.substr(kSourcePrefix.size());
}

// Returns the camera id in `source.<id>.start_ms` keys, or empty otherwise.
std::string_view start_offset_camera_id(std::string_view key) {
  constexpr std::string_view suffix{".start_ms"};
  const auto camera_id = source_camera_id(key);
  if (!camera_id.ends_with(suffix)) {
    return {};
  }
  return camera_id.substr(0, camera_id.size() - suffix.size());
}

// Manifest hashes are keyed by the same camera ids as the config, so the lane
// is resolved against the config's declared order rather than a fixed table.
std::string_view manifest_camera_id(std::string_view key) {
  constexpr std::string_view suffix{"_sha256"};
  const auto camera_id = source_camera_id(key);
  if (!camera_id.ends_with(suffix)) {
    return {};
  }
  return camera_id.substr(0, camera_id.size() - suffix.size());
}

bool valid_sha256(std::string_view value) noexcept {
  if (value.size() != 64) {
    return false;
  }
  for (const auto character : value) {
    const bool decimal = character >= '0' && character <= '9';
    const bool lower = character >= 'a' && character <= 'f';
    const bool upper = character >= 'A' && character <= 'F';
    if (!decimal && !lower && !upper) {
      return false;
    }
  }
  return true;
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

// Expands `${NAME}` tokens in a path-valued config entry using the process
// environment. Reports errors with the config file path and line number so
// they read the same as other config parse errors.
std::string expand_environment_variables(const std::filesystem::path& path,
                                          std::size_t line,
                                          std::string_view value) {
  std::string expanded;
  expanded.reserve(value.size());
  std::size_t index = 0;
  while (index < value.size()) {
    const auto dollar = value.find("${", index);
    if (dollar == std::string_view::npos) {
      expanded.append(value.substr(index));
      break;
    }
    expanded.append(value.substr(index, dollar - index));
    const auto close = value.find('}', dollar + 2);
    if (close == std::string_view::npos) {
      config_error(path, line, "unterminated '${' in value");
    }
    const auto name = value.substr(dollar + 2, close - (dollar + 2));
    if (name.empty()) {
      config_error(path, line, "empty variable name in '${}'");
    }
    const auto name_string = std::string(name);
    const auto* const resolved = std::getenv(name_string.c_str());
    if (resolved == nullptr) {
      config_error(path, line,
                   "environment variable '" + name_string + "' is not set");
    }
    expanded.append(resolved);
    index = close + 1;
  }
  return expanded;
}

std::filesystem::path parse_config_path(const std::filesystem::path& path,
                                        std::size_t line,
                                        std::string_view value) {
  return std::filesystem::path{expand_environment_variables(path, line, value)};
}

}  // namespace

AppConfig load_config(const std::filesystem::path& path) {
  std::ifstream input(path);
  if (!input) {
    config_error(path, 0, "cannot open config");
  }

  AppConfig config;
  std::unordered_set<std::string> seen_keys;
  // Lanes are filled in the order the config declares them, so the file defines
  // both the camera ids and their left-to-right order.
  std::vector<SourceConfig> declared_sources;
  // (camera id, line, offset) collected as encountered; matched to lanes below.
  std::vector<std::tuple<std::string, std::size_t, std::chrono::milliseconds>>
      start_offsets;
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

    const auto offset_camera_id = start_offset_camera_id(key);
    const auto camera_id =
        offset_camera_id.empty() ? source_camera_id(key) : std::string_view{};
    if (!offset_camera_id.empty()) {
      // Recorded once per lane, in whatever order relative to its `source.<id>`
      // line; resolved after the whole file is read so either order works.
      start_offsets.emplace_back(
          std::string(offset_camera_id), line_number,
          std::chrono::milliseconds{
              parse_config_unsigned(path, line_number, key, value)});
    } else if (!camera_id.empty()) {
      if (declared_sources.size() >= kMaxCameras) {
        config_error(path, line_number,
                     "at most " + std::to_string(kMaxCameras) +
                         " source keys are supported");
      }
      declared_sources.push_back(
          {std::string(camera_id), parse_config_path(path, line_number, value),
           std::chrono::milliseconds{0}});
    } else if (key == "backend") {
      config.backend = value;
    } else if (key == "mode") {
      config.mode = parse_config_enum(path, line_number, key, value, parse_mode);
    } else if (key == "stage") {
      config.stage =
          parse_config_enum(path, line_number, key, value, parse_stage);
    } else if (key == "asset") {
      config.asset_path = parse_config_path(path, line_number, value);
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
    } else if (key == "loop_sources") {
      config.loop_sources = parse_config_boolean(path, line_number, key, value);
    } else if (key == "encode_path") {
      config.encode_path = parse_config_path(path, line_number, value);
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

  if (declared_sources.empty()) {
    config_error(path, line_number + 1,
                 "config must declare at least one 'source.<camera-id>' key");
  }
  config.sources = {};
  for (std::size_t index = 0; index < declared_sources.size(); ++index) {
    config.sources[index] = std::move(declared_sources[index]);
  }
  config.source_count = static_cast<std::uint32_t>(declared_sources.size());
  for (const auto& [camera_id, line, offset] : start_offsets) {
    bool matched = false;
    for (std::uint32_t index = 0; index < config.source_count; ++index) {
      if (config.sources[index].camera_id == camera_id) {
        config.sources[index].start_offset = offset;
        matched = true;
        break;
      }
    }
    if (!matched) {
      config_error(path, line,
                   "'source." + camera_id +
                       ".start_ms' names a camera with no 'source." +
                       camera_id + "' key");
    }
  }
  // stream_count defaults to every declared lane; an explicit --stream-count
  // override may still narrow it for benchmark sweeps.
  config.stream_count = config.source_count;
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
    } else if (name == "--preview-visible") {
      config.preview_visible = parse_boolean(value, name);
    } else if (name == "--encode") {
      config.encode = parse_boolean(value, name);
    } else if (name == "--diagnostic-replacement") {
      config.diagnostic_replacement = parse_boolean(value, name);
    } else if (name == "--loop") {
      config.loop_sources = parse_boolean(value, name);
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
      if (count == 0 || count > kMaxCameras) {
        throw std::runtime_error("--stream-count must be between 1 and " +
                                 std::to_string(kMaxCameras));
      }
      config.stream_count = count;
    } else if (name == "--metrics") {
      config.metrics_path = parse_cli_path(value, name);
    } else if (name == "--fps") {
      // Data-independent render cadence: sets the realtime pacing target
      // directly (fps_num/1). The latest-frame mailbox reuses or drops source
      // frames to meet it, so it is independent of the input clip's fps.
      const auto fps = parse_unsigned(value, "--fps");
      if (fps == 0) {
        throw std::runtime_error("--fps must be a positive integer");
      }
      config.fps_num = fps;
      config.fps_den = 1;
    } else if (name == "--fps-num") {
      config.fps_num = parse_unsigned(value, "--fps-num");
    } else if (name == "--fps-den") {
      const auto den = parse_unsigned(value, "--fps-den");
      if (den == 0) {
        throw std::runtime_error("--fps-den must be a positive integer");
      }
      config.fps_den = den;
    } else if (name == "--benchmark-manifest") {
      config.benchmark_manifest_path = parse_cli_path(value, name);
    } else {
      throw std::runtime_error("unknown command-line option '" +
                               std::string(argument) + "'");
    }
  }
  return config;
}

BenchmarkManifest load_benchmark_manifest(const std::filesystem::path& path) {
  std::ifstream input(path);
  if (!input) {
    config_error(path, 0, "cannot open benchmark manifest");
  }

  BenchmarkManifest manifest;
  std::unordered_set<std::string> seen_keys;
  // Hashes fill lanes in declaration order, mirroring how load_config assigns
  // camera ids; the count is checked against the config by the caller.
  std::size_t source_count = 0;
  bool seen_run_id = false;
  bool seen_asset = false;
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
    if (!seen_keys.emplace(key).second) {
      config_error(path, line_number,
                   "duplicate key '" + std::string(key) + "'");
    }
    if (key == "run_id") {
      if (value.empty()) {
        config_error(path, line_number, "run_id must not be empty");
      }
      manifest.run_id = value;
      seen_run_id = true;
      continue;
    }
    if (key == "asset_sha256") {
      if (!valid_sha256(value)) {
        config_error(path, line_number,
                     "asset_sha256 must be exactly 64 hexadecimal digits");
      }
      manifest.asset_sha256 = value;
      seen_asset = true;
      continue;
    }
    const auto camera_id = manifest_camera_id(key);
    if (camera_id.empty()) {
      config_error(path, line_number,
                   "unknown key '" + std::string(key) + "'");
    }
    if (source_count >= kMaxCameras) {
      config_error(path, line_number,
                   "at most " + std::to_string(kMaxCameras) +
                       " source hashes are supported");
    }
    if (!valid_sha256(value)) {
      config_error(path, line_number, std::string(key) +
                                          " must be exactly 64 hexadecimal digits");
    }
    manifest.source_sha256[source_count] = value;
    ++source_count;
  }

  if (!seen_run_id) {
    config_error(path, line_number + 1, "missing key 'run_id'");
  }
  if (!seen_asset) {
    config_error(path, line_number + 1, "missing key 'asset_sha256'");
  }
  if (source_count == 0) {
    config_error(path, line_number + 1,
                 "manifest must declare at least one "
                 "'source.<camera-id>_sha256' key");
  }
  return manifest;
}

std::optional<BenchmarkManifest> resolve_benchmark_manifest(
    const AppConfig& config) {
  if (config.benchmark_manifest_path.empty()) {
    if (config.mode == RunMode::benchmark) {
      throw std::runtime_error(
          "benchmark mode requires --benchmark-manifest=PATH");
    }
    return std::nullopt;
  }
  return load_benchmark_manifest(config.benchmark_manifest_path);
}

}  // namespace swim::core
