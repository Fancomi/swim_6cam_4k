#include "swim/metal/mp4_source.hpp"
#include "swim/metal/videotoolbox_decoder.hpp"

#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>

#include <array>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

namespace {

struct Options final {
  std::vector<std::filesystem::path> paths;
  std::uint64_t frames = 120;
  std::chrono::seconds duration{10};
  bool six = false;
};

[[noreturn]] void usage() {
  throw std::invalid_argument(
      "usage: metal_decode_probe <cam1.mp4> [--frames N] "
      "[--six] [--seconds N]\n"
      "       metal_decode_probe <cam1.mp4> ... <cam6.mp4> --six");
}

std::uint64_t positive_number(std::string_view value) {
  std::size_t parsed = 0;
  const auto text = std::string{value};
  const auto result = std::stoull(text, &parsed);
  if (parsed != text.size() || result == 0) {
    usage();
  }
  return result;
}

Options parse_options(int argc, char** argv) {
  Options options;
  for (int index = 1; index < argc; ++index) {
    const std::string_view argument{argv[index]};
    if (argument == "--six") {
      options.six = true;
    } else if (argument == "--frames") {
      if (++index >= argc) {
        usage();
      }
      options.frames = positive_number(argv[index]);
    } else if (argument == "--seconds") {
      if (++index >= argc) {
        usage();
      }
      options.duration =
          std::chrono::seconds{positive_number(argv[index])};
    } else if (argument.starts_with("--")) {
      usage();
    } else {
      options.paths.emplace_back(argument);
    }
  }
  if (options.paths.empty()) {
    usage();
  }
  if (!options.six && options.paths.size() != 1) {
    usage();
  }
  if (options.six && options.paths.size() == 1) {
    const auto input = options.paths.front().string();
    const auto marker = input.find("cam1");
    if (marker == std::string::npos) {
      throw std::invalid_argument(
          "--six path derivation requires a filename containing cam1");
    }
    options.paths.clear();
    for (unsigned camera = 1; camera <= 6; ++camera) {
      auto path = input;
      path.replace(marker, 4, "cam" + std::to_string(camera));
      options.paths.emplace_back(std::move(path));
    }
  }
  if (options.six && options.paths.size() != 6) {
    usage();
  }
  return options;
}

std::shared_ptr<swim::metal::MetalContext> make_context() {
  auto context = std::make_shared<swim::metal::MetalContext>();
  context->device = MTLCreateSystemDefaultDevice();
  if (context->device == nil) {
    throw std::runtime_error("Metal device is unavailable");
  }
  context->command_queue = [context->device newCommandQueue];
  if (context->command_queue == nil) {
    throw std::runtime_error("cannot create Metal command queue");
  }
  const auto status = CVMetalTextureCacheCreate(
      kCFAllocatorDefault, nullptr, context->device, nullptr,
      &context->texture_cache);
  if (status != kCVReturnSuccess || context->texture_cache == nullptr) {
    throw std::runtime_error("cannot create shared Metal texture cache");
  }
  return context;
}

void require(bool condition, const std::string& message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

void validate_frame(const swim::core::FrameLease& frame,
                    std::size_t camera, std::uint64_t& previous_sequence) {
  const auto& metadata = frame.metadata();
  require(metadata.camera_index == camera, "camera metadata crossed lanes");
  require(metadata.width == 3840, "decoded width is not 3840");
  require(metadata.height == 2160, "decoded height is not 2160");
  require(metadata.pixel_format ==
              swim::core::PixelFormat::nv12_video_range,
          "decoded format is not video-range NV12");
  require(metadata.color_matrix == swim::core::ColorMatrix::bt709,
          "decoded color matrix is not BT.709");
  require(metadata.sequence > previous_sequence,
          "published sequence is not monotonic");
  require(metadata.decoder_generation != 0,
          "frame has no decoder generation");
  require(metadata.pts_timescale > 0, "frame has no valid PTS timescale");
  auto* surface = static_cast<swim::metal::MetalDecodedSurface*>(
      frame.native(swim::metal::kMetalDecodedSurfaceTag));
  require(surface != nullptr, "decoded lease has no native surface");
  require(surface->camera_index == camera,
          "native decoded surface crossed camera lanes");
  require(surface->luma != nil && surface->chroma != nil,
          "decoded surface has no Metal plane views");
  previous_sequence = metadata.sequence;
}

int run(const Options& options) {
  constexpr std::size_t kMaximumLanes = 6;
  const auto lane_count = options.six ? kMaximumLanes : std::size_t{1};
  auto context = make_context();
  std::array<swim::core::LatestFrameMailbox, kMaximumLanes> mailboxes;
  std::array<swim::core::RuntimeCounters, kMaximumLanes> counters;
  std::vector<std::unique_ptr<swim::metal::Mp4VideoToolboxSource>> sources;
  sources.reserve(lane_count);
  for (std::size_t camera = 0; camera < lane_count; ++camera) {
    require(std::filesystem::is_regular_file(options.paths[camera]),
            "input MP4 does not exist: " + options.paths[camera].string());
    swim::core::SourceConfig config{
        "cam" + std::to_string(camera + 1), options.paths[camera]};
    sources.push_back(std::make_unique<swim::metal::Mp4VideoToolboxSource>(
        context, std::move(config), static_cast<std::uint32_t>(camera),
        mailboxes[camera], counters[camera],
        options.six ? swim::core::RunMode::realtime
                    : swim::core::RunMode::benchmark,
        options.six
            ? std::chrono::duration_cast<std::chrono::milliseconds>(
                  options.duration)
            : std::chrono::milliseconds{0}));
  }
  for (auto& source : sources) {
    source->start();
  }

  std::array<std::uint64_t, kMaximumLanes> previous_sequence{};
  std::array<std::uint64_t, kMaximumLanes> consumed{};
  const auto started_at = std::chrono::steady_clock::now();
  const auto deadline = started_at +
      (options.six ? options.duration + std::chrono::seconds{5}
                   : std::chrono::seconds{90});
  while (std::chrono::steady_clock::now() < deadline) {
    for (std::size_t camera = 0; camera < lane_count; ++camera) {
      swim::core::FrameLease frame;
      if (mailboxes[camera].consume_latest(frame)) {
        validate_frame(frame, camera, previous_sequence[camera]);
        ++consumed[camera];
      }
    }
    if (!options.six &&
        counters[0].published.load(std::memory_order_relaxed) >=
            options.frames &&
        consumed[0] != 0) {
      break;
    }
    if (options.six &&
        std::chrono::steady_clock::now() >= started_at + options.duration) {
      break;
    }
    bool early_failure = false;
    for (const auto& source : sources) {
      early_failure = early_failure || source->failed();
    }
    if (early_failure) {
      break;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds{1});
  }

  for (auto& source : sources) {
    source->stop();
  }
  for (auto& source : sources) {
    source->wait();
  }
  for (std::size_t camera = 0; camera < lane_count; ++camera) {
    swim::core::FrameLease frame;
    if (mailboxes[camera].consume_latest(frame)) {
      validate_frame(frame, camera, previous_sequence[camera]);
      ++consumed[camera];
    }
    require(!sources[camera]->failed(),
            "lane failed: " + sources[camera]->last_error());
    require(sources[camera]->using_hardware_acceleration(),
            "lane did not use hardware VideoToolbox decode");
    const auto published =
        counters[camera].published.load(std::memory_order_relaxed);
    require(published >= (options.six ? 1 : options.frames),
            "lane published too few decoded frames");
    require(consumed[camera] != 0, "lane produced no consumable frame");
    require(counters[camera].decoded_pixel_host_copies.load(
                std::memory_order_relaxed) == 0,
            "decode path performed a pixel host copy");
    std::cout << "cam" << camera + 1
              << " 3840x2160 30000/1001 hardware=true callbacks="
              << counters[camera].decoded.load(std::memory_order_relaxed)
              << " published=" << published
              << " consumed=" << consumed[camera]
              << " host_copies=0\n";
  }
  return EXIT_SUCCESS;
}

}  // namespace

int main(int argc, char** argv) {
  try {
    return run(parse_options(argc, argv));
  } catch (const std::exception& error) {
    std::cerr << "metal_decode_probe: " << error.what() << '\n';
    return EXIT_FAILURE;
  }
}
