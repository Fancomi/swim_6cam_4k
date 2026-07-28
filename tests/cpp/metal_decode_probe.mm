#include "swim/metal/mp4_source.hpp"
#include "swim/metal/videotoolbox_decoder.hpp"

#include <swim/core/hot_path_allocations.hpp>

#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>

#include <algorithm>
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

void verify_surface_capacity_contract(
    const std::shared_ptr<swim::metal::MetalContext>& context,
    const std::filesystem::path& path) {
  for (std::uint32_t capacity = 1; capacity < 4; ++capacity) {
    swim::core::LatestFrameMailbox mailbox;
    swim::core::RuntimeCounters counters;
    bool rejected = false;
    try {
      swim::metal::Mp4VideoToolboxSource source(
          context, {"cam1", path}, 0, mailbox, counters,
          swim::core::RunMode::benchmark, std::chrono::milliseconds{0}, 16,
          capacity);
    } catch (const std::invalid_argument&) {
      rejected = true;
    }
    require(rejected,
            "decoded surface capacity below four was not rejected");
  }
}

void validate_frame(const swim::core::FrameLease& frame,
                    std::size_t camera, std::uint64_t& previous_sequence,
                    CMTime& previous_pts) {
  const auto& metadata = frame.metadata();
  require(metadata.camera_index == camera, "camera metadata crossed lanes");
  // Frame geometry follows the stream (4K pool mp4, 720p underwater TS); only
  // the NV12 wrapping constraints are universal.
  require(metadata.width > 0 && (metadata.width & 1U) == 0,
          "decoded width is not a positive even number");
  require(metadata.height > 0 && (metadata.height & 1U) == 0,
          "decoded height is not a positive even number");
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
  const auto pts = CMTimeMake(metadata.pts_value,
                              static_cast<std::int32_t>(metadata.pts_timescale));
  require(CMTIME_IS_VALID(pts) && CMTIME_IS_NUMERIC(pts),
          "frame PTS is invalid");
  if (CMTIME_IS_VALID(previous_pts)) {
    require(CMTimeCompare(pts, previous_pts) > 0,
            "consumed frame PTS is not strictly increasing");
  }
  auto* surface = static_cast<swim::metal::MetalDecodedSurface*>(
      frame.native(swim::metal::kMetalDecodedSurfaceTag));
  require(surface != nullptr, "decoded lease has no native surface");
  require(surface->camera_index == camera,
          "native decoded surface crossed camera lanes");
  require(surface->luma != nil && surface->chroma != nil,
          "decoded surface has no Metal plane views");
  require(surface->pixel_buffer != nullptr,
          "decoded surface has no retained pixel buffer");
  require(CVPixelBufferGetIOSurface(surface->pixel_buffer) != nullptr,
          "decoded pixel buffer is not IOSurface-backed");
  auto matrix = CVBufferCopyAttachment(surface->pixel_buffer,
                                       kCVImageBufferYCbCrMatrixKey, nullptr);
  const bool is_bt709 = matrix != nullptr &&
                        CFEqual(matrix,
                                kCVImageBufferYCbCrMatrix_ITU_R_709_2);
  if (matrix != nullptr) {
    CFRelease(matrix);
  }
  require(is_bt709, "decoded pixel buffer has no BT.709 matrix attachment");
  previous_sequence = metadata.sequence;
  previous_pts = pts;
}

int run(const Options& options) {
  constexpr std::size_t kMaximumLanes = 6;
  const auto lane_count = options.six ? kMaximumLanes : std::size_t{1};
  const auto minimum_callbacks = options.six
      ? std::max<std::uint64_t>(
            1, static_cast<std::uint64_t>(
                   static_cast<double>(options.duration.count()) * 30000.0 /
                   1001.0 * 0.9))
      : options.frames;
  const auto warmup_frames = std::min<std::uint64_t>(
      30, std::max<std::uint64_t>(1, minimum_callbacks / 4));
  auto context = make_context();
  verify_surface_capacity_contract(context, options.paths.front());
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
  std::array<CMTime, kMaximumLanes> previous_pts;
  previous_pts.fill(kCMTimeInvalid);
  std::array<std::uint64_t, kMaximumLanes> first_sequence{};
  std::array<CMTime, kMaximumLanes> first_pts;
  first_pts.fill(kCMTimeInvalid);
  std::array<std::uint64_t, kMaximumLanes> consumed{};
  std::array<std::uint32_t, kMaximumLanes> lane_width{};
  std::array<std::uint32_t, kMaximumLanes> lane_height{};
  std::uint64_t allocation_baseline{};
  bool allocation_baseline_set = false;
  const auto started_at = std::chrono::steady_clock::now();
  const auto deadline = started_at +
      (options.six ? options.duration + std::chrono::seconds{5}
                   : std::chrono::seconds{90});
  while (std::chrono::steady_clock::now() < deadline) {
    for (std::size_t camera = 0; camera < lane_count; ++camera) {
      swim::core::FrameLease frame;
      if (mailboxes[camera].consume_latest(frame)) {
        validate_frame(frame, camera, previous_sequence[camera],
                       previous_pts[camera]);
        lane_width[camera] = frame.metadata().width;
        lane_height[camera] = frame.metadata().height;
        if (!CMTIME_IS_VALID(first_pts[camera])) {
          first_sequence[camera] = frame.metadata().sequence;
          first_pts[camera] = previous_pts[camera];
        }
        ++consumed[camera];
      }
    }
    if (!allocation_baseline_set) {
      bool warmed = true;
      for (std::size_t camera = 0; camera < lane_count; ++camera) {
        warmed = warmed &&
                 counters[camera].published.load(std::memory_order_relaxed) >=
                     warmup_frames;
      }
      if (warmed) {
        allocation_baseline = swim::core::hot_path_allocation_count();
        allocation_baseline_set = true;
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
      validate_frame(frame, camera, previous_sequence[camera],
                     previous_pts[camera]);
      lane_width[camera] = frame.metadata().width;
      lane_height[camera] = frame.metadata().height;
      if (!CMTIME_IS_VALID(first_pts[camera])) {
        first_sequence[camera] = frame.metadata().sequence;
        first_pts[camera] = previous_pts[camera];
      }
      ++consumed[camera];
    }
    require(!sources[camera]->failed(),
            "lane failed: " + sources[camera]->last_error());
    require(sources[camera]->using_hardware_acceleration(),
            "lane did not use hardware VideoToolbox decode");
    const auto published =
        counters[camera].published.load(std::memory_order_relaxed);
    require(published >= minimum_callbacks,
            "lane published too few decoded frames");
    const auto stats = sources[camera]->decoder_stats();
    require(stats.callbacks >= minimum_callbacks,
            "lane delivered too few VideoToolbox callbacks");
    require(stats.callbacks == stats.submitted,
            "not every submitted decode delivered a callback");
    require(stats.errors == 0, "lane reported a decoder error");
    require(counters[camera].native_decode_tickets.load(
                std::memory_order_relaxed) == 16,
            "lane did not create exactly 16 fixed decode tickets");
    require(counters[camera].native_callback_wrappers.load(
                std::memory_order_relaxed) == 1,
            "lane unexpectedly rebuilt its callback/session wrapper");
    require(counters[camera].native_texture_wrappers.load(
                std::memory_order_relaxed) == published * 2,
            "lane did not create exactly two texture wrappers per publish");
    require(counters[camera].pool_exhaustion.load(
                std::memory_order_relaxed) == 0,
            "lane exhausted a fixed decode pool");
    require(consumed[camera] != 0, "lane produced no consumable frame");
    require(counters[camera].decoded_pixel_host_copies.load(
                std::memory_order_relaxed) == 0,
            "decode path performed a pixel host copy");
    require(CMTIME_IS_VALID(first_pts[camera]) &&
                CMTimeCompare(previous_pts[camera], first_pts[camera]) > 0,
            "lane has insufficient PTS span for FPS measurement");
    const auto seconds =
        CMTimeGetSeconds(CMTimeSubtract(previous_pts[camera],
                                       first_pts[camera]));
    const auto measured_fps =
        static_cast<double>(previous_sequence[camera] -
                            first_sequence[camera]) /
        seconds;
    require(measured_fps >= 29.8 && measured_fps <= 30.1,
            "measured decoded FPS is outside 29.8..30.1");
    std::cout << "cam" << camera + 1 << ' ' << lane_width[camera] << 'x'
              << lane_height[camera] << " measured_fps=" << measured_fps
              << " hardware=true callbacks=" << stats.callbacks
              << " minimum_callbacks=" << minimum_callbacks
              << " published=" << published
              << " dropped=" << stats.dropped
              << " consumed=" << consumed[camera]
              << " host_copies=0\n";
  }
  require(allocation_baseline_set,
          "decode did not reach the hot-path allocation warmup");
  const auto final_allocations = swim::core::hot_path_allocation_count();
  require(final_allocations == allocation_baseline,
          "steady decode hot path allocated after warmup (baseline=" +
              std::to_string(allocation_baseline) + ", final=" +
              std::to_string(final_allocations) + ")");
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
