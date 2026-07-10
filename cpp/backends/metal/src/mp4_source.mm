#include <swim/metal/mp4_source.hpp>

#include <swim/core/camera_health.hpp>
#include <swim/metal/videotoolbox_decoder.hpp>

#import <AVFoundation/AVFoundation.h>
#import <CoreMedia/CoreMedia.h>
#import <dispatch/dispatch.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <exception>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>

namespace swim::metal {
namespace {

constexpr std::size_t kMaximumLaneErrorBytes = 512;

std::string ns_error(NSString* prefix, NSError* error) {
  const char* detail =
      error == nil ? nullptr : error.localizedDescription.UTF8String;
  return std::string(prefix.UTF8String) +
         (detail == nullptr ? std::string{} : ": " + std::string(detail));
}

bool valid_pts(CMTime pts) noexcept {
  return CMTIME_IS_VALID(pts) && CMTIME_IS_NUMERIC(pts) &&
         pts.timescale > 0;
}

class RetainedVideoFormat final {
 public:
  explicit RetainedVideoFormat(CMVideoFormatDescriptionRef value)
      : value_(value) {
    if (value_ != nullptr) {
      CFRetain(value_);
    }
  }
  ~RetainedVideoFormat() {
    if (value_ != nullptr) {
      CFRelease(value_);
    }
  }
  RetainedVideoFormat(const RetainedVideoFormat&) = delete;
  RetainedVideoFormat& operator=(const RetainedVideoFormat&) = delete;

  CMVideoFormatDescriptionRef get() const noexcept { return value_; }

  void reset(CMVideoFormatDescriptionRef value) noexcept {
    if (value != nullptr) {
      CFRetain(value);
    }
    if (value_ != nullptr) {
      CFRelease(value_);
    }
    value_ = value;
  }

 private:
  CMVideoFormatDescriptionRef value_{};
};

}  // namespace

class Mp4VideoToolboxSource::Impl final {
 public:
  Impl(std::shared_ptr<MetalContext> context,
       swim::core::SourceConfig source, std::uint32_t camera_index,
       swim::core::LatestFrameMailbox& mailbox,
       swim::core::RuntimeCounters& counters, swim::core::RunMode mode,
       std::chrono::milliseconds run_duration,
       std::uint32_t ticket_capacity, std::uint32_t surface_capacity)
      : context_(std::move(context)),
        source_(std::move(source)),
        camera_index_(camera_index),
        mailbox_(mailbox),
        counters_(counters),
        mode_(mode),
        run_duration_(run_duration),
        ticket_capacity_(ticket_capacity),
        surface_capacity_(surface_capacity) {
    if (context_ == nullptr || context_->device == nil ||
        context_->texture_cache == nullptr) {
      throw std::invalid_argument("MP4 source requires a valid Metal context");
    }
    if (source_.path.empty()) {
      throw std::invalid_argument("MP4 source path must not be empty");
    }
    if (camera_index_ >= 6) {
      throw std::invalid_argument("MP4 source camera index must be below six");
    }
    if (run_duration_.count() < 0) {
      throw std::invalid_argument("MP4 source duration must not be negative");
    }
    if (ticket_capacity_ == 0 || ticket_capacity_ > 64 ||
        surface_capacity_ == 0 || surface_capacity_ > 64) {
      throw std::invalid_argument(
          "MP4 decoder pool capacities must be between 1 and 64");
    }
  }

  ~Impl() {
    stop();
    wait();
  }

  void start() {
    std::lock_guard lock(thread_mutex_);
    if (worker_.joinable() || running_.load(std::memory_order_acquire)) {
      throw std::logic_error("MP4 source is already started");
    }
    stop_requested_.store(false, std::memory_order_release);
    failed_.store(false, std::memory_order_release);
    completed_.store(false, std::memory_order_release);
    hardware_.store(false, std::memory_order_release);
    generation_.store(0, std::memory_order_release);
    {
      std::lock_guard error_lock(error_mutex_);
      last_error_.clear();
    }
    {
      std::lock_guard stats_lock(stats_mutex_);
      decoder_stats_ = {};
    }
    worker_ = std::thread([this] { run(); });
  }

  void stop() noexcept {
    stop_requested_.store(true, std::memory_order_release);
  }

  void wait() {
    std::thread worker;
    {
      std::lock_guard lock(thread_mutex_);
      if (!worker_.joinable()) {
        return;
      }
      worker = std::move(worker_);
    }
    worker.join();
  }

  bool running() const noexcept {
    return running_.load(std::memory_order_acquire);
  }

  bool failed() const noexcept {
    return failed_.load(std::memory_order_acquire);
  }

  bool using_hardware_acceleration() const noexcept {
    return hardware_.load(std::memory_order_acquire);
  }

  std::uint64_t decoder_generation() const noexcept {
    return generation_.load(std::memory_order_acquire);
  }

  VideoToolboxDecoderStats decoder_stats() const {
    std::lock_guard lock(stats_mutex_);
    return decoder_stats_;
  }

  std::string last_error() const {
    std::lock_guard lock(error_mutex_);
    return last_error_;
  }

 private:
  void store_decoder_stats(VideoToolboxDecoderStats stats) {
    std::lock_guard lock(stats_mutex_);
    decoder_stats_ = stats;
  }

  [[noreturn]] static void throw_decoder_error(
      const VideoToolboxDecoder& decoder) {
    throw std::runtime_error(
        "recoverable VideoToolbox decode error (OSStatus " +
        std::to_string(decoder.recoverable_error_status()) + ")");
  }

  void set_error(std::string message, bool fatal) noexcept {
    try {
      if (message.size() > kMaximumLaneErrorBytes) {
        message.resize(kMaximumLaneErrorBytes);
      }
      std::lock_guard lock(error_mutex_);
      last_error_ = std::move(message);
    } catch (...) {
    }
    if (fatal) {
      failed_.store(true, std::memory_order_release);
    }
  }

  bool sleep_interruptibly(std::chrono::milliseconds delay) const noexcept {
    const auto until = std::chrono::steady_clock::now() + delay;
    while (!stop_requested_.load(std::memory_order_acquire)) {
      const auto now = std::chrono::steady_clock::now();
      if (now >= until) {
        return true;
      }
      std::this_thread::sleep_for(
          std::min(std::chrono::duration_cast<std::chrono::milliseconds>(
                       until - now),
                   std::chrono::milliseconds{10}));
    }
    return false;
  }

  void pace(CMTime pts, CMTime& first_pts,
            std::chrono::steady_clock::time_point& first_wall) const {
    if (mode_ != swim::core::RunMode::realtime || !valid_pts(pts)) {
      return;
    }
    if (!valid_pts(first_pts)) {
      first_pts = pts;
      first_wall = std::chrono::steady_clock::now();
      return;
    }
    const auto seconds = CMTimeGetSeconds(CMTimeSubtract(pts, first_pts));
    if (!std::isfinite(seconds) || seconds <= 0.0) {
      return;
    }
    const auto target = first_wall + std::chrono::duration_cast<
        std::chrono::steady_clock::duration>(
        std::chrono::duration<double>{seconds});
    while (!stop_requested_.load(std::memory_order_acquire) &&
           std::chrono::steady_clock::now() < target) {
      std::this_thread::sleep_until(
          std::min(target, std::chrono::steady_clock::now() +
                               std::chrono::milliseconds{10}));
    }
  }

  void run_reader_once(VideoToolboxDecoder& decoder,
                       swim::core::CameraHealthTracker& health) {
    @autoreleasepool {
      const auto path = source_.path.string();
      NSString* ns_path = [NSString stringWithUTF8String:path.c_str()];
      if (ns_path == nil) {
        throw std::runtime_error("MP4 source path is not valid UTF-8");
      }
      AVURLAsset* asset = [AVURLAsset
          URLAssetWithURL:[NSURL fileURLWithPath:ns_path]
                  options:nil];
      __block NSArray<AVAssetTrack*>* tracks = nil;
      __block NSError* track_error = nil;
      dispatch_semaphore_t tracks_loaded = dispatch_semaphore_create(0);
      [asset loadTracksWithMediaType:AVMediaTypeVideo
                  completionHandler:^(NSArray<AVAssetTrack*>* loaded_tracks,
                                      NSError* error) {
                    tracks = loaded_tracks;
                    track_error = error;
                    dispatch_semaphore_signal(tracks_loaded);
                  }];
      dispatch_semaphore_wait(tracks_loaded, DISPATCH_TIME_FOREVER);
      if (track_error != nil) {
        throw std::runtime_error(
            ns_error(@"cannot load MP4 video tracks", track_error));
      }
      AVAssetTrack* track = tracks.firstObject;
      if (track == nil) {
        throw std::runtime_error("MP4 source has no video track");
      }
      CMVideoFormatDescriptionRef initial_format = nullptr;
      for (id value in track.formatDescriptions) {
        auto description = (__bridge CMVideoFormatDescriptionRef)value;
        if (CMFormatDescriptionGetMediaType(description) != kCMMediaType_Video) {
          continue;
        }
        if (CMFormatDescriptionGetMediaSubType(description) !=
            kCMVideoCodecType_H264) {
          throw std::runtime_error("MP4 video track is not H.264/avc1");
        }
        initial_format = description;
        break;
      }
      if (initial_format == nullptr) {
        throw std::runtime_error("MP4 video track has no H.264 format");
      }

      NSError* reader_error = nil;
      AVAssetReader* reader =
          [[AVAssetReader alloc] initWithAsset:asset error:&reader_error];
      if (reader == nil) {
        throw std::runtime_error(ns_error(@"cannot create AVAssetReader",
                                          reader_error));
      }
      AVAssetReaderTrackOutput* output =
          [[AVAssetReaderTrackOutput alloc] initWithTrack:track
                                          outputSettings:nil];
      output.alwaysCopiesSampleData = NO;
      if (![reader canAddOutput:output]) {
        throw std::runtime_error("cannot attach compressed video output");
      }
      [reader addOutput:output];

      decoder.configure(initial_format);
      hardware_.store(decoder.using_hardware_acceleration(),
                      std::memory_order_release);
      generation_.store(decoder.generation(), std::memory_order_release);
      if (!hardware_.load(std::memory_order_acquire)) {
        throw std::runtime_error(
            "VideoToolbox hardware acceleration is required");
      }
      if (![reader startReading]) {
        throw std::runtime_error(
            ns_error(@"cannot start compressed MP4 reading", reader.error));
      }

      RetainedVideoFormat submitted_format{initial_format};
      CMTime first_pts = kCMTimeInvalid;
      std::chrono::steady_clock::time_point first_wall{};
      while (!stop_requested_.load(std::memory_order_acquire)) {
        if (run_duration_.count() > 0 &&
            std::chrono::steady_clock::now() >= deadline_) {
          break;
        }
        CMSampleBufferRef sample = [output copyNextSampleBuffer];
        if (sample == nullptr) {
          break;
        }
        const auto sample_count = CMSampleBufferGetNumSamples(sample);
        const auto sample_bytes = CMSampleBufferGetTotalSampleSize(sample);
        if (sample_count == 0 && sample_bytes == 0) {
          // AVAssetReader may emit a ready, timing-only marker before the
          // first compressed access unit. It is not a decodable frame.
          CFRelease(sample);
          continue;
        }
        if (sample_count != 1) {
          counters_.malformed.fetch_add(1, std::memory_order_relaxed);
          CFRelease(sample);
          throw std::runtime_error(
              "compressed MP4 sample must contain exactly one access unit");
        }
        if (CMSampleBufferGetImageBuffer(sample) != nullptr ||
            sample_bytes == 0 || !CMSampleBufferDataIsReady(sample)) {
          const auto has_image_buffer =
              CMSampleBufferGetImageBuffer(sample) != nullptr;
          const auto has_data_buffer =
              CMSampleBufferGetDataBuffer(sample) != nullptr;
          const auto data_ready = CMSampleBufferDataIsReady(sample);
          counters_.malformed.fetch_add(1, std::memory_order_relaxed);
          CFRelease(sample);
          throw std::runtime_error(
              "AVAssetReader output was not compressed sample data "
              "(image_buffer=" +
              std::to_string(has_image_buffer) + ", data_buffer=" +
              std::to_string(has_data_buffer) + ", samples=" +
              std::to_string(sample_count) + ", bytes=" +
              std::to_string(sample_bytes) + ", ready=" +
              std::to_string(data_ready) + ")");
        }
        counters_.received.fetch_add(1, std::memory_order_relaxed);
        health.on_frame(std::chrono::steady_clock::now());
        const auto pts = CMSampleBufferGetPresentationTimeStamp(sample);
        pace(pts, first_pts, first_wall);
        auto format = static_cast<CMVideoFormatDescriptionRef>(
            CMSampleBufferGetFormatDescription(sample));
        if (format == nullptr ||
            CMFormatDescriptionGetMediaSubType(format) !=
                kCMVideoCodecType_H264) {
          counters_.malformed.fetch_add(1, std::memory_order_relaxed);
          CFRelease(sample);
          continue;
        }
        if (!CMFormatDescriptionEqual(format, submitted_format.get())) {
          decoder.configure(format);
          generation_.store(decoder.generation(), std::memory_order_release);
          submitted_format.reset(format);
        }
        const auto sequence =
            next_sequence_.fetch_add(1, std::memory_order_relaxed) + 1;
        if (decoder.has_recoverable_error()) {
          CFRelease(sample);
          throw_decoder_error(decoder);
        }
        const auto submit_result = decoder.decode(
            sample, decoder.generation(), sequence, pts);
        CFRelease(sample);
        if (submit_result == DecodeSubmitResult::recoverable_error ||
            decoder.has_recoverable_error()) {
          throw_decoder_error(decoder);
        }
        if (submit_result == DecodeSubmitResult::stale_or_invalid) {
          counters_.malformed.fetch_add(1, std::memory_order_relaxed);
        }
      }
      decoder.drain();
      if (decoder.has_recoverable_error()) {
        throw_decoder_error(decoder);
      }
      generation_.store(decoder.generation(), std::memory_order_release);
      if (stop_requested_.load(std::memory_order_acquire) ||
          (run_duration_.count() > 0 &&
           std::chrono::steady_clock::now() >= deadline_)) {
        [reader cancelReading];
        return;
      }
      if (reader.status == AVAssetReaderStatusCompleted) {
        if (run_duration_.count() > 0) {
          throw std::runtime_error(
              "MP4 reached EOF before the configured run duration");
        }
        completed_.store(true, std::memory_order_release);
        return;
      }
      throw std::runtime_error(
          ns_error(@"compressed MP4 reader failed", reader.error));
    }
  }

  void run() noexcept {
    running_.store(true, std::memory_order_release);
    const auto start_time = std::chrono::steady_clock::now();
    deadline_ = run_duration_.count() == 0
                    ? std::chrono::steady_clock::time_point::max()
                    : start_time + run_duration_;
    try {
      VideoToolboxDecoder decoder(context_, camera_index_, ticket_capacity_,
                                  surface_capacity_, mailbox_, counters_);
      try {
        swim::core::CameraHealthTracker health;
        while (!stop_requested_.load(std::memory_order_acquire) &&
               std::chrono::steady_clock::now() < deadline_) {
          try {
            run_reader_once(decoder, health);
            break;
          } catch (const std::exception& error) {
            decoder.invalidate();
            generation_.store(decoder.generation(), std::memory_order_release);
            set_error(error.what(), false);
            if (std::string{error.what()}.find("EOF before") !=
                    std::string::npos ||
                run_duration_.count() == 0 ||
                std::chrono::steady_clock::now() >= deadline_) {
              throw;
            }
            counters_.reconnects.fetch_add(1, std::memory_order_relaxed);
            if (!sleep_interruptibly(health.next_reconnect_delay())) {
              break;
            }
          }
        }
      } catch (...) {
        store_decoder_stats(decoder.stats());
        throw;
      }
      store_decoder_stats(decoder.stats());
    } catch (const std::exception& error) {
      set_error(error.what(), true);
    } catch (...) {
      set_error("unknown MP4 lane failure", true);
    }
    running_.store(false, std::memory_order_release);
  }

  std::shared_ptr<MetalContext> context_;
  swim::core::SourceConfig source_;
  std::uint32_t camera_index_{};
  swim::core::LatestFrameMailbox& mailbox_;
  swim::core::RuntimeCounters& counters_;
  swim::core::RunMode mode_;
  std::chrono::milliseconds run_duration_;
  std::uint32_t ticket_capacity_{};
  std::uint32_t surface_capacity_{};
  std::chrono::steady_clock::time_point deadline_{};
  mutable std::mutex thread_mutex_;
  std::thread worker_;
  std::atomic_bool stop_requested_{false};
  std::atomic_bool running_{false};
  std::atomic_bool failed_{false};
  std::atomic_bool completed_{false};
  std::atomic_bool hardware_{false};
  std::atomic_uint64_t generation_{0};
  std::atomic_uint64_t next_sequence_{0};
  mutable std::mutex error_mutex_;
  std::string last_error_;
  mutable std::mutex stats_mutex_;
  VideoToolboxDecoderStats decoder_stats_;
};

Mp4VideoToolboxSource::Mp4VideoToolboxSource(
    std::shared_ptr<MetalContext> context, swim::core::SourceConfig source,
    std::uint32_t camera_index, swim::core::LatestFrameMailbox& mailbox,
    swim::core::RuntimeCounters& counters, swim::core::RunMode mode,
    std::chrono::milliseconds run_duration, std::uint32_t ticket_capacity,
    std::uint32_t surface_capacity)
    : impl_(std::make_unique<Impl>(
          std::move(context), std::move(source), camera_index, mailbox,
          counters, mode, run_duration, ticket_capacity, surface_capacity)) {}

Mp4VideoToolboxSource::~Mp4VideoToolboxSource() = default;

void Mp4VideoToolboxSource::start() { impl_->start(); }
void Mp4VideoToolboxSource::stop() noexcept { impl_->stop(); }
void Mp4VideoToolboxSource::wait() { impl_->wait(); }
bool Mp4VideoToolboxSource::running() const noexcept { return impl_->running(); }
bool Mp4VideoToolboxSource::failed() const noexcept { return impl_->failed(); }
bool Mp4VideoToolboxSource::using_hardware_acceleration() const noexcept {
  return impl_->using_hardware_acceleration();
}
std::uint64_t Mp4VideoToolboxSource::decoder_generation() const noexcept {
  return impl_->decoder_generation();
}
VideoToolboxDecoderStats Mp4VideoToolboxSource::decoder_stats() const {
  return impl_->decoder_stats();
}
std::string Mp4VideoToolboxSource::last_error() const {
  return impl_->last_error();
}

}  // namespace swim::metal
