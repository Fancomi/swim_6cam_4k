#include <swim/metal/mp4_source.hpp>

#include <swim/core/camera_capacity.hpp>
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

enum class LaneFailureKind : std::uint8_t {
  fatal,
  recoverable,
};

class LaneFailure final : public std::runtime_error {
 public:
  LaneFailure(LaneFailureKind kind, std::string message)
      : std::runtime_error(std::move(message)), kind_(kind) {}

  LaneFailureKind kind() const noexcept { return kind_; }

 private:
  LaneFailureKind kind_;
};

enum class ReaderOutcome : std::uint8_t {
  stopped_or_duration_reached,
  normal_eof,
  // The pass covered the configured loop period; restart from the aligned start.
  loop_period_reached,
};

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
       std::uint32_t ticket_capacity, std::uint32_t surface_capacity,
       swim::core::RunLifecycle* lifecycle, bool loop_sources,
       std::chrono::milliseconds loop_period, bool stop_at_eof)
      : context_(std::move(context)),
        source_(std::move(source)),
        camera_index_(camera_index),
        mailbox_(mailbox),
        counters_(counters),
        mode_(mode),
        run_duration_(run_duration),
        ticket_capacity_(ticket_capacity),
        surface_capacity_(surface_capacity),
        lifecycle_(lifecycle),
        loop_sources_(loop_sources),
        loop_period_(loop_period),
        stop_at_eof_(stop_at_eof) {
    if (context_ == nullptr || context_->device == nil ||
        context_->texture_cache == nullptr) {
      throw std::invalid_argument("MP4 source requires a valid Metal context");
    }
    if (source_.path.empty()) {
      throw std::invalid_argument("MP4 source path must not be empty");
    }
    if (camera_index_ >= swim::core::kMaxCameras) {
      throw std::invalid_argument(
          "MP4 source camera index must be below kMaxCameras");
    }
    if (run_duration_.count() < 0) {
      throw std::invalid_argument("MP4 source duration must not be negative");
    }
    if (ticket_capacity_ == 0 || ticket_capacity_ > 64 ||
        surface_capacity_ < 4 || surface_capacity_ > 64) {
      throw std::invalid_argument(
          "MP4 decoder ticket capacity must be 1..64 and surface capacity "
          "must be 4..64");
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
    throw LaneFailure{
        LaneFailureKind::recoverable,
        "recoverable VideoToolbox decode error (OSStatus " +
            std::to_string(decoder.recoverable_error_status()) + ")"};
  }

  static void configure_decoder(
      VideoToolboxDecoder& decoder,
      CMVideoFormatDescriptionRef format_description) {
    try {
      decoder.configure(format_description);
    } catch (const DecoderConfigurationError& error) {
      const auto kind =
          error.failure() == DecoderConfigurationFailure::operational
              ? LaneFailureKind::recoverable
              : LaneFailureKind::fatal;
      throw LaneFailure{kind, error.what()};
    } catch (const std::invalid_argument& error) {
      throw LaneFailure{LaneFailureKind::fatal, error.what()};
    }
  }

  static void drain_decoder(VideoToolboxDecoder& decoder) {
    try {
      decoder.drain();
    } catch (const std::exception& error) {
      throw LaneFailure{LaneFailureKind::recoverable, error.what()};
    }
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
    const auto until = std::min(std::chrono::steady_clock::now() + delay,
                                effective_deadline());
    while (!termination_requested(std::chrono::steady_clock::now())) {
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
            std::chrono::steady_clock::time_point& first_wall) {
    if (mode_ != swim::core::RunMode::realtime || !valid_pts(pts)) {
      return;
    }
    if (!valid_pts(first_pts)) {
      first_pts = pts;
      // Looping passes inherit the wall origin recorded when the pass began, so
      // the cadence continues instead of restarting and emitting a burst.
      const auto now = std::chrono::steady_clock::now();
      if (pass_wall_origin_ == std::chrono::steady_clock::time_point{}) {
        first_wall = now;
      } else {
        // Re-opening the reader and re-decoding up to the aligned start costs
        // real time (hundreds of ms on lanes with a large offset). Charging the
        // new pass from the nominal boundary would make its first frames late
        // and provoke a catch-up burst, so the origin absorbs the overrun.
        first_wall = std::max(pass_wall_origin_, now);
        pass_wall_origin_ = first_wall;
      }
      return;
    }
    const auto seconds = CMTimeGetSeconds(CMTimeSubtract(pts, first_pts));
    if (!std::isfinite(seconds) || seconds <= 0.0) {
      return;
    }
    const auto target = first_wall + std::chrono::duration_cast<
        std::chrono::steady_clock::duration>(
        std::chrono::duration<double>{seconds});
    while (!termination_requested(std::chrono::steady_clock::now()) &&
           std::chrono::steady_clock::now() < target) {
      std::this_thread::sleep_until(
          std::min(target, std::chrono::steady_clock::now() +
                               std::chrono::milliseconds{10}));
    }
  }

  // How far the wall clock advances per pass. With a shared loop period every
  // lane advances by exactly that; without one each lane advances by whatever
  // its own file spanned, which is why unequal clips drift.
  std::chrono::steady_clock::duration pass_wall_advance() const noexcept {
    return std::chrono::duration_cast<std::chrono::steady_clock::duration>(
        loop_period_.count() > 0 ? loop_period_ : last_pass_span_);
  }

  // True once this pass has covered the common loop period, measured from the
  // lane's aligned start. Every lane shares the period, so they wrap on the same
  // content boundary even though their files differ in usable length by tens of
  // milliseconds. Zero period means "play to the file's own end".
  bool pass_period_elapsed(CMTime pts, CMTime first_emitted_pts) const noexcept {
    if (!loop_sources_ || loop_period_.count() <= 0 ||
        !valid_pts(pts) || !valid_pts(first_emitted_pts)) {
      return false;
    }
    const auto seconds =
        CMTimeGetSeconds(CMTimeSubtract(pts, first_emitted_pts));
    if (!std::isfinite(seconds)) {
      return false;
    }
    return seconds >= std::chrono::duration<double>(loop_period_).count();
  }

  // True once `pts` reaches the lane's aligned start. `first_sample_pts` is
  // latched on the first call so the offset is relative to the file, matching
  // how the manifest's keyframe timestamp anchors frame 0.
  //
  // Every pass skips, including loops. The offset is what puts this lane's
  // frame 0 of a pass on the common time axis; replaying from the file's own
  // frame 0 instead would restart each lane at its own keyframe, which are up
  // to ~3s apart. Re-decoding the skipped span costs real time, and that is
  // absorbed as a brief late start rather than as content misalignment.
  bool past_start_offset(CMTime pts, CMTime& first_sample_pts) const noexcept {
    if (source_.start_offset.count() <= 0) {
      return true;
    }
    if (!valid_pts(pts)) {
      return true;
    }
    if (!valid_pts(first_sample_pts)) {
      first_sample_pts = pts;
    }
    const auto seconds =
        CMTimeGetSeconds(CMTimeSubtract(pts, first_sample_pts));
    if (!std::isfinite(seconds)) {
      return true;
    }
    const auto offset_seconds =
        std::chrono::duration<double>(source_.start_offset).count();
    return seconds >= offset_seconds;
  }

  ReaderOutcome run_reader_once(VideoToolboxDecoder& decoder,
                                swim::core::CameraHealthTracker& health) {
    @autoreleasepool {
      const auto path = source_.path.string();
      NSString* ns_path = [NSString stringWithUTF8String:path.c_str()];
      if (ns_path == nil) {
        throw LaneFailure{LaneFailureKind::fatal,
                          "MP4 source path is not valid UTF-8"};
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
      while (dispatch_semaphore_wait(
                 tracks_loaded,
                 dispatch_time(DISPATCH_TIME_NOW,
                               10 * static_cast<std::int64_t>(NSEC_PER_MSEC))) !=
             0) {
        if (termination_requested(std::chrono::steady_clock::now())) {
          return ReaderOutcome::stopped_or_duration_reached;
        }
      }
      if (track_error != nil) {
        throw LaneFailure{
            LaneFailureKind::recoverable,
            ns_error(@"cannot load MP4 video tracks", track_error)};
      }
      AVAssetTrack* track = tracks.firstObject;
      if (track == nil) {
        throw LaneFailure{LaneFailureKind::fatal,
                          "MP4 source has no video track"};
      }
      CMVideoFormatDescriptionRef initial_format = nullptr;
      for (id value in track.formatDescriptions) {
        auto description = (__bridge CMVideoFormatDescriptionRef)value;
        if (CMFormatDescriptionGetMediaType(description) != kCMMediaType_Video) {
          continue;
        }
        if (CMFormatDescriptionGetMediaSubType(description) !=
            kCMVideoCodecType_H264) {
          throw LaneFailure{LaneFailureKind::fatal,
                            "MP4 video track is not H.264/avc1"};
        }
        initial_format = description;
        break;
      }
      if (initial_format == nullptr) {
        throw LaneFailure{LaneFailureKind::fatal,
                          "MP4 video track has no H.264 format"};
      }

      NSError* reader_error = nil;
      AVAssetReader* reader =
          [[AVAssetReader alloc] initWithAsset:asset error:&reader_error];
      if (reader == nil) {
        throw LaneFailure{
            LaneFailureKind::recoverable,
            ns_error(@"cannot create AVAssetReader", reader_error)};
      }
      AVAssetReaderTrackOutput* output =
          [[AVAssetReaderTrackOutput alloc] initWithTrack:track
                                          outputSettings:nil];
      output.alwaysCopiesSampleData = NO;
      if (![reader canAddOutput:output]) {
        throw LaneFailure{LaneFailureKind::fatal,
                          "cannot attach compressed video output"};
      }
      [reader addOutput:output];

      configure_decoder(decoder, initial_format);
      hardware_.store(decoder.using_hardware_acceleration(),
                      std::memory_order_release);
      generation_.store(decoder.generation(), std::memory_order_release);
      if (!hardware_.load(std::memory_order_acquire)) {
        throw LaneFailure{LaneFailureKind::fatal,
                          "VideoToolbox hardware acceleration is required"};
      }
      if (![reader startReading]) {
        throw LaneFailure{
            LaneFailureKind::recoverable,
            ns_error(@"cannot start compressed MP4 reading", reader.error)};
      }

      RetainedVideoFormat submitted_format{initial_format};
      CMTime first_pts = kCMTimeInvalid;
      // The clip's own first PTS; the aligned start is measured from it.
      CMTime first_sample_pts = kCMTimeInvalid;
      // Last PTS this pass published, used to measure the pass span when no
      // shared loop period was configured.
      CMTime last_emitted_pts = kCMTimeInvalid;
      std::chrono::steady_clock::time_point first_wall{};
      while (!termination_requested(std::chrono::steady_clock::now())) {
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
          throw LaneFailure{
              LaneFailureKind::fatal,
              "compressed MP4 sample must contain exactly one access unit"};
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
          throw LaneFailure{
              LaneFailureKind::fatal,
              "AVAssetReader output was not compressed sample data "
              "(image_buffer=" +
                  std::to_string(has_image_buffer) + ", data_buffer=" +
                  std::to_string(has_data_buffer) + ", samples=" +
                  std::to_string(sample_count) + ", bytes=" +
                  std::to_string(sample_bytes) + ", ready=" +
                  std::to_string(data_ready) + ")"};
        }
        counters_.received.fetch_add(1, std::memory_order_relaxed);
        counters_.camera_received[camera_index_].fetch_add(
            1, std::memory_order_relaxed);
        health.on_frame(std::chrono::steady_clock::now());
        const auto pts = CMSampleBufferGetPresentationTimeStamp(sample);
        // Skip forward to this lane's aligned start before publishing. The
        // samples before it still decode (suppressed output) so the reference
        // chain stays intact, and pacing starts at the aligned frame so the
        // realtime cadence is not charged for the skipped span.
        const bool emit = past_start_offset(pts, first_sample_pts);
        if (emit && pass_period_elapsed(pts, first_pts)) {
          // Every lane has covered the shared period; end the pass here so the
          // restart happens on the same content boundary for all of them.
          CFRelease(sample);
          [reader cancelReading];
          return ReaderOutcome::loop_period_reached;
        }
        if (emit) {
          pace(pts, first_pts, first_wall);
          last_emitted_pts = pts;
        }
        auto format = static_cast<CMVideoFormatDescriptionRef>(
            CMSampleBufferGetFormatDescription(sample));
        if (format == nullptr ||
            CMFormatDescriptionGetMediaSubType(format) !=
                kCMVideoCodecType_H264) {
          counters_.malformed.fetch_add(1, std::memory_order_relaxed);
          CFRelease(sample);
          throw LaneFailure{LaneFailureKind::fatal,
                            "compressed sample is not H.264/avc1"};
        }
        if (!CMFormatDescriptionEqual(format, submitted_format.get())) {
          configure_decoder(decoder, format);
          generation_.store(decoder.generation(), std::memory_order_release);
          submitted_format.reset(format);
        }
        if (decoder.has_recoverable_error()) {
          CFRelease(sample);
          throw_decoder_error(decoder);
        }
        const auto submit_result =
            decoder.decode(sample, decoder.generation(), emit);
        CFRelease(sample);
        if (submit_result == DecodeSubmitResult::recoverable_error ||
            decoder.has_recoverable_error()) {
          throw_decoder_error(decoder);
        }
        if (submit_result == DecodeSubmitResult::stale_or_invalid) {
          counters_.malformed.fetch_add(1, std::memory_order_relaxed);
        }
      }
      drain_decoder(decoder);
      if (decoder.has_recoverable_error()) {
        throw_decoder_error(decoder);
      }
      generation_.store(decoder.generation(), std::memory_order_release);
      const auto finished_at = std::chrono::steady_clock::now();
      if (termination_requested(finished_at)) {
        [reader cancelReading];
        return ReaderOutcome::stopped_or_duration_reached;
      }
      if (reader.status == AVAssetReaderStatusCompleted) {
        if (loop_sources_) {
          if (valid_pts(first_pts) && valid_pts(last_emitted_pts)) {
            const auto seconds =
                CMTimeGetSeconds(CMTimeSubtract(last_emitted_pts, first_pts));
            if (std::isfinite(seconds) && seconds > 0.0) {
              last_pass_span_ =
                  std::chrono::duration_cast<std::chrono::milliseconds>(
                      std::chrono::duration<double>{seconds});
            }
          }
          // Looping is the caller's answer to "the clip is shorter than the
          // run": restart rather than substituting the black replacement frame.
          return ReaderOutcome::loop_period_reached;
        }
        if (stop_at_eof_) {
          // The clips are the whole run: finishing them is success, so end the
          // run instead of reporting this lane as failed.
          if (lifecycle_ != nullptr) {
            lifecycle_->request_stop();
          }
          return ReaderOutcome::stopped_or_duration_reached;
        }
        if (lifecycle_ != nullptr) {
          const auto disposition = lifecycle_->classify_eof(finished_at);
          if (disposition == swim::core::SourceEofDisposition::normal_after_stop ||
              disposition ==
                  swim::core::SourceEofDisposition::normal_after_deadline) {
            return ReaderOutcome::stopped_or_duration_reached;
          }
          throw LaneFailure{
              LaneFailureKind::fatal,
              swim::core::source_eof_failure_message(disposition)};
        }
        if (run_duration_.count() > 0) {
          throw LaneFailure{
              LaneFailureKind::fatal,
              "MP4 reached EOF before the configured run duration"};
        }
        return ReaderOutcome::normal_eof;
      }
      throw LaneFailure{
          LaneFailureKind::recoverable,
          ns_error(@"compressed MP4 reader failed", reader.error)};
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
        // The pass wall origin anchors pacing across loops so a restart does not
        // reset the cadence clock; it is set once, before the first pass.
        pass_wall_origin_ = std::chrono::steady_clock::now();
        while (!termination_requested(std::chrono::steady_clock::now())) {
          try {
            const auto outcome = run_reader_once(decoder, health);
            if (outcome == ReaderOutcome::loop_period_reached) {
              // Reconfiguring on the next pass advances the decoder generation,
              // which resets its PTS monotonicity guard — the restarted clip
              // replays timestamps this lane already published.
              decoder.invalidate();
              generation_.store(decoder.generation(),
                                std::memory_order_release);
              // Advance the wall origin by one period so the next pass paces
              // against its own start. Leaving it at the run's start would put
              // every target in the past, and the lane would decode flat out.
              pass_wall_origin_ += pass_wall_advance();
              continue;
            }
            if (outcome == ReaderOutcome::normal_eof) {
              completed_.store(true, std::memory_order_release);
            }
            break;
          } catch (const LaneFailure& error) {
            decoder.invalidate();
            generation_.store(decoder.generation(), std::memory_order_release);
            set_error(error.what(), false);
            if (error.kind() == LaneFailureKind::fatal) {
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

  std::chrono::steady_clock::time_point effective_deadline() const noexcept {
    return lifecycle_ == nullptr ? deadline_ : lifecycle_->deadline();
  }

  bool termination_requested(
      std::chrono::steady_clock::time_point now) const noexcept {
    return stop_requested_.load(std::memory_order_acquire) ||
           (lifecycle_ != nullptr ? lifecycle_->should_stop(now)
                                  : now >= deadline_);
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
  swim::core::RunLifecycle* lifecycle_{};
  const bool loop_sources_{false};
  // Common content period every lane restarts on. Zero falls back to each
  // file's own end, which only stays in sync for equal-length clips.
  const std::chrono::milliseconds loop_period_{0};
  const bool stop_at_eof_{false};
  // Wall time the current pass began, so pacing stays continuous across a loop
  // instead of restarting the clock and emitting a burst.
  std::chrono::steady_clock::time_point pass_wall_origin_{};
  // Content span the finished pass actually emitted; only consulted when no
  // shared loop period was configured.
  std::chrono::milliseconds last_pass_span_{0};
  std::chrono::steady_clock::time_point deadline_{};
  mutable std::mutex thread_mutex_;
  std::thread worker_;
  std::atomic_bool stop_requested_{false};
  std::atomic_bool running_{false};
  std::atomic_bool failed_{false};
  std::atomic_bool completed_{false};
  std::atomic_bool hardware_{false};
  std::atomic_uint64_t generation_{0};
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
    std::uint32_t surface_capacity, swim::core::RunLifecycle* lifecycle,
    bool loop_sources, std::chrono::milliseconds loop_period, bool stop_at_eof)
    : impl_(std::make_unique<Impl>(
          std::move(context), std::move(source), camera_index, mailbox,
          counters, mode, run_duration, ticket_capacity, surface_capacity,
          lifecycle, loop_sources, loop_period, stop_at_eof)) {}

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
