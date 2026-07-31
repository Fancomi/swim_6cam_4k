#pragma once

#include <swim/core/config.hpp>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <limits>
#include <thread>

namespace swim::core {

// The one wall instant every lane maps its media t=0 to.
//
// Lanes are started sequentially and each anchors on its own thread, so without
// this each lane's first decoded frame defines its own zero. A lane that took
// 400ms longer to open its reader then paces every later frame against that late
// origin and stays 400ms behind the others for the whole run — measured on the
// 6-camera pool line as a 13-frame deficit at 1s that was still exactly 13
// frames at 20s. Sharing the origin makes a late lane's early targets already
// overdue, so it decodes flat out until it catches up instead of holding the lag.
class SharedLaneOrigin final {
 public:
  using Clock = std::chrono::steady_clock;

  // The first lane to call this wins; every later caller gets that instant back,
  // however much later it arrives.
  Clock::time_point latch(Clock::time_point candidate) noexcept {
    const auto candidate_ns = to_ns(candidate);
    std::int64_t expected = 0;
    if (origin_ns_.compare_exchange_strong(expected, candidate_ns,
                                           std::memory_order_acq_rel,
                                           std::memory_order_acquire)) {
      return candidate;
    }
    return Clock::time_point{std::chrono::nanoseconds{expected}};
  }

  // The wall origin for pass `pass_index`, agreed across lanes.
  //
  // Wrapping costs real time — re-opening the reader and re-decoding up to the
  // aligned start — and lanes pay different amounts, so each pass needs an origin
  // that absorbs the overrun without letting lanes diverge. Two ways of getting
  // this wrong were measured on 16-lane underwater (2026-07-31):
  //   * each lane raising its own origin to its own arrival: the lag compounds,
  //     published spread grew ~4 frames per wrap without bound;
  //   * nobody raising it at all: the nominal origin falls behind reality, every
  //     lane is permanently overdue and decodes flat out, and the render collapsed
  //     from 30fps to 20fps with `reused` climbing into the thousands.
  // The first lane to reach pass N sets that pass's origin for everyone, so the
  // overrun is absorbed once, identically for all lanes.
  Clock::time_point latch_pass(std::uint64_t pass_index,
                               Clock::time_point candidate) noexcept {
    if (pass_index == 0) {
      return latch(candidate);
    }
    const auto candidate_ns = to_ns(candidate);
    auto seen = pass_index_.load(std::memory_order_acquire);
    while (seen < pass_index) {
      if (pass_index_.compare_exchange_weak(seen, pass_index,
                                            std::memory_order_acq_rel,
                                            std::memory_order_acquire)) {
        pass_origin_ns_.store(candidate_ns, std::memory_order_release);
        return candidate;
      }
    }
    if (seen == pass_index) {
      return Clock::time_point{std::chrono::nanoseconds{
          pass_origin_ns_.load(std::memory_order_acquire)}};
    }
    // A lane that is a whole pass behind the leader: give it the leader's origin
    // so its targets are already overdue and it catches up rather than pacing
    // against a boundary the others left long ago.
    return Clock::time_point{std::chrono::nanoseconds{
        pass_origin_ns_.load(std::memory_order_acquire)}};
  }

 private:
  static std::int64_t to_ns(Clock::time_point value) noexcept {
    return std::chrono::duration_cast<std::chrono::nanoseconds>(
               value.time_since_epoch())
        .count();
  }

  // 0 means "not yet latched"; steady_clock epoch nanoseconds otherwise.
  std::atomic_int64_t origin_ns_{0};
  std::atomic_uint64_t pass_index_{0};
  std::atomic_int64_t pass_origin_ns_{0};
};

// One lane's playback timing, shared by every backend's source.
//
// The three decode APIs disagree about almost everything — CMTime, MF's 100ns
// units, FFmpeg's rational timebase — but nothing above the timestamp is
// API-specific: gating on the aligned start, pacing to the wall clock, wrapping
// on a shared loop period and carrying the cadence across a wrap are the same
// rules on every platform. Keeping one copy here is what makes them the same
// rules; three copies is how the Windows backends ended up without the loop
// period and pass origin the Metal one had, so recorded lanes drifted apart by
// tens of milliseconds on every pass.
//
// Callers convert their own timestamps to nanoseconds on the media timeline and
// pass kInvalidPts for "no usable timestamp".
class LanePacer final {
 public:
  using Clock = std::chrono::steady_clock;
  static constexpr std::int64_t kInvalidPts =
      std::numeric_limits<std::int64_t>::min();

  // `shared_origin` is the run-wide anchor for media t=0; pass nullptr to give
  // this lane its own (which is only right when there is nothing to align to).
  LanePacer(RunMode mode, std::chrono::milliseconds start_offset,
            bool loop_sources, std::chrono::milliseconds loop_period,
            SharedLaneOrigin* shared_origin = nullptr) noexcept
      : mode_(mode),
        start_offset_ns_(std::chrono::nanoseconds{start_offset}.count()),
        loop_sources_(loop_sources),
        loop_period_ns_(std::chrono::nanoseconds{loop_period}.count()),
        shared_origin_(shared_origin) {}

  // Called once before the first pass, then again for each pass. The first call
  // anchors the run; later ones only clear the per-pass timestamp latches, so
  // the wall origin advanced by advance_pass() survives into the next pass.
  //
  // The clip origin is NOT cleared: it is a property of the file, latched on the
  // very first frame the lane ever saw. Re-latching it per pass makes the gate
  // depend on whatever frame the rewound reader happens to deliver first, which
  // measurably destabilised the panorama — over 5 runs one showed a lane 229ms
  // stale and snapshot_age_spread p99 at 304ms, against 0/7 runs and a 97ms
  // worst case once the origin persists (measured 2026-07-31, 16-lane
  // underwater, 40s each).
  void begin_pass() noexcept {
    first_pts_ns_ = kInvalidPts;
    last_emitted_pts_ns_ = kInvalidPts;
    first_wall_ = Clock::time_point{};
  }

  void begin_run(Clock::time_point now) noexcept {
    // Leave the wall origin unset: the first paced frame adopts the run-wide
    // shared origin instead, so a lane that opens late does not anchor on its own
    // late arrival. `now` is accepted for symmetry with begin_pass callers and to
    // keep the signature honest about when the run began.
    static_cast<void>(now);
    pass_wall_origin_ = Clock::time_point{};
    clip_origin_pts_ns_ = kInvalidPts;
    pass_index_ = 0;
    begin_pass();
  }

  // Where this lane's aligned start sits on the clip's own timeline, or
  // kInvalidPts before the first frame has been seen. Exposed for diagnostics
  // and for callers that want to report the alignment they resolved; backends
  // deliberately do NOT seek here when replaying — see the comment on
  // mf_source.cpp's seek_to_start for the measurement that settled that.
  std::int64_t aligned_start_pts_ns() const noexcept {
    if (clip_origin_pts_ns_ == kInvalidPts) {
      return kInvalidPts;
    }
    return clip_origin_pts_ns_ + start_offset_ns_;
  }

  // True once `pts_ns` reaches this lane's aligned start. The first frame the
  // lane ever sees latches the clip's own origin, so the offset is measured from
  // the file, matching how the manifest's keyframe timestamp anchors frame 0.
  //
  // Every pass gates, loops included. The offset is what puts this lane's frame
  // 0 of a pass on the common time axis; replaying from the file's own frame 0
  // would restart each lane at its own keyframe, and those are up to ~3s apart.
  bool past_start_offset(std::int64_t pts_ns) noexcept {
    if (pts_ns == kInvalidPts) {
      return true;
    }
    if (clip_origin_pts_ns_ == kInvalidPts) {
      clip_origin_pts_ns_ = pts_ns;
    }
    if (start_offset_ns_ <= 0) {
      return true;
    }
    return pts_ns - clip_origin_pts_ns_ >= start_offset_ns_;
  }

  // True once this pass has covered the common loop period, measured from the
  // lane's aligned start. Every lane shares the period, so they wrap on the same
  // content boundary even though their files differ in usable length by tens of
  // milliseconds. A zero period means "play to the file's own end", which only
  // stays in sync for equal-length clips.
  bool pass_period_elapsed(std::int64_t pts_ns) const noexcept {
    if (!loop_sources_ || loop_period_ns_ <= 0 || pts_ns == kInvalidPts ||
        first_pts_ns_ == kInvalidPts) {
      return false;
    }
    return pts_ns - first_pts_ns_ >= loop_period_ns_;
  }

  // Sleep until this frame's wall-clock slot. `is_running` is polled while
  // waiting so a stop request or the run deadline cuts the sleep short; pass a
  // predicate that returns false when the lane should give up.
  template <class RunningPredicate>
  void pace(std::int64_t pts_ns, RunningPredicate is_running) {
    if (mode_ != RunMode::realtime || pts_ns == kInvalidPts) {
      return;
    }
    last_emitted_pts_ns_ = pts_ns;
    if (first_pts_ns_ == kInvalidPts) {
      first_pts_ns_ = pts_ns;
      const auto now = Clock::now();
      // Every pass takes its origin from the run-wide anchor: pass 0 from the
      // first lane to produce a frame, later passes from the first lane to reach
      // that pass. A lane that opened late, or spent longer re-decoding its
      // skipped span, therefore finds its early targets already overdue and
      // decodes flat out until level — instead of freezing its own lag in
      // (which compounds per wrap) or pacing against a nominal boundary the rest
      // of the panorama has long left behind (which starves the renderer).
      first_wall_ = shared_origin_ == nullptr
                        ? (pass_wall_origin_ == Clock::time_point{}
                               ? now
                               : std::max(pass_wall_origin_, now))
                        : shared_origin_->latch_pass(pass_index_, now);
      pass_wall_origin_ = first_wall_;
      return;
    }
    const auto elapsed = pts_ns - first_pts_ns_;
    if (elapsed <= 0) {
      return;
    }
    const auto target =
        first_wall_ + std::chrono::duration_cast<Clock::duration>(
                          std::chrono::nanoseconds{elapsed});
    while (is_running() && Clock::now() < target) {
      std::this_thread::sleep_until(
          std::min(target, Clock::now() + std::chrono::milliseconds{10}));
    }
  }

  // Called when a pass ends and the lane is about to replay the clip. Advances
  // the wall origin by one period so the next pass paces against its own start;
  // leaving it at the run's start would put every target in the past and the
  // lane would decode flat out.
  //
  // With a shared loop period every lane advances by exactly that. Without one
  // each lane advances by whatever its own file spanned — which is the drift the
  // period exists to remove, so the fallback is only right for equal clips.
  void advance_pass() noexcept {
    ++pass_index_;
    auto advance_ns = loop_period_ns_;
    if (advance_ns <= 0) {
      advance_ns = last_pass_span_ns();
    }
    if (advance_ns > 0) {
      pass_wall_origin_ += std::chrono::duration_cast<Clock::duration>(
          std::chrono::nanoseconds{advance_ns});
    }
    begin_pass();
  }

  std::int64_t start_offset_ns() const noexcept { return start_offset_ns_; }

 private:
  // How much media time this pass actually published, for the no-period case.
  std::int64_t last_pass_span_ns() const noexcept {
    if (first_pts_ns_ == kInvalidPts || last_emitted_pts_ns_ == kInvalidPts) {
      return 0;
    }
    return std::max<std::int64_t>(0, last_emitted_pts_ns_ - first_pts_ns_);
  }

  RunMode mode_;
  std::int64_t start_offset_ns_;
  bool loop_sources_;
  std::int64_t loop_period_ns_;
  SharedLaneOrigin* shared_origin_;
  // Which replay pass this lane is on; 0 is the first play-through. Used to ask
  // the shared anchor for the right pass's origin.
  std::uint64_t pass_index_{0};
  std::int64_t first_pts_ns_{kInvalidPts};
  std::int64_t clip_origin_pts_ns_{kInvalidPts};
  std::int64_t last_emitted_pts_ns_{kInvalidPts};
  Clock::time_point first_wall_{};
  Clock::time_point pass_wall_origin_{};
};

}  // namespace swim::core
