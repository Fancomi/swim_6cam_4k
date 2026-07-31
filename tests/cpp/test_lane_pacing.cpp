#include "test_support.hpp"

#include <swim/core/lane_pacing.hpp>

#include <chrono>
#include <cstdint>

namespace {

using swim::core::LanePacer;
using swim::core::RunMode;
using namespace std::chrono_literals;

constexpr std::int64_t kSecond = 1'000'000'000;

std::int64_t ms(std::int64_t value) { return value * 1'000'000; }

// A pacer whose sleeps return immediately, so tests exercise the arithmetic
// rather than the wall clock.
auto never_running = [] { return false; };

}  // namespace

TEST_CASE(pacer_without_an_offset_admits_the_first_frame) {
  LanePacer pacer(RunMode::realtime, 0ms, false, 0ms);
  pacer.begin_run(LanePacer::Clock::now());
  CHECK(pacer.past_start_offset(0));
  CHECK(pacer.past_start_offset(ms(33)));
}

TEST_CASE(pacer_gates_until_the_aligned_start_measured_from_the_clip) {
  // The offset is relative to the clip's own first timestamp, not to zero: a
  // recorded .ts starts at whatever keyframe the recorder happened to write.
  LanePacer pacer(RunMode::realtime, 500ms, false, 0ms);
  pacer.begin_run(LanePacer::Clock::now());
  const auto clip_origin = 90 * kSecond;
  CHECK(!pacer.past_start_offset(clip_origin));
  CHECK(!pacer.past_start_offset(clip_origin + ms(499)));
  CHECK(pacer.past_start_offset(clip_origin + ms(500)));
  CHECK(pacer.past_start_offset(clip_origin + ms(600)));
}

TEST_CASE(pacer_replays_the_offset_on_every_pass) {
  // Each pass must skip again. Replaying from the file's own frame 0 instead
  // would restart each lane at its own keyframe, and those are seconds apart.
  LanePacer pacer(RunMode::realtime, 500ms, true, 12'000ms);
  pacer.begin_run(LanePacer::Clock::now());
  CHECK(!pacer.past_start_offset(0));
  CHECK(pacer.past_start_offset(ms(500)));
  pacer.advance_pass();
  CHECK(!pacer.past_start_offset(0));
  CHECK(pacer.past_start_offset(ms(500)));
}

TEST_CASE(pacer_keeps_the_clip_origin_across_passes) {
  // The origin is a property of the file, not of the pass. Re-latching it per
  // pass makes the gate depend on whichever frame the rewound reader delivers
  // first: a lane that resumes mid-GOP would measure its offset from there and
  // skip too far, which showed up as a lane hundreds of ms stale.
  LanePacer pacer(RunMode::realtime, 500ms, true, 12'000ms);
  pacer.begin_run(LanePacer::Clock::now());
  const auto clip_origin = 90 * kSecond;
  CHECK(!pacer.past_start_offset(clip_origin));
  CHECK_EQ(pacer.aligned_start_pts_ns(), clip_origin + ms(500));
  pacer.advance_pass();
  // The origin survives, so the aligned start is unchanged...
  CHECK_EQ(pacer.aligned_start_pts_ns(), clip_origin + ms(500));
  // ...and a pass that resumes late still gates on the file's own timeline
  // rather than re-anchoring to the frame it happened to receive.
  CHECK(pacer.past_start_offset(clip_origin + ms(700)));

  // begin_run() is the only thing that clears it: a fresh reader after a lane
  // failure may open a stream whose timestamps start somewhere else entirely.
  pacer.begin_run(LanePacer::Clock::now());
  CHECK_EQ(pacer.aligned_start_pts_ns(), LanePacer::kInvalidPts);
}

TEST_CASE(pacer_wraps_on_the_shared_period_not_the_clip_end) {
  // This is the drift fix: every lane wraps at the same content boundary even
  // though the files differ in usable length by tens of milliseconds.
  LanePacer pacer(RunMode::realtime, 0ms, true, 11'952ms);
  pacer.begin_run(LanePacer::Clock::now());
  pacer.pace(0, never_running);
  CHECK(!pacer.pass_period_elapsed(ms(11'951)));
  CHECK(pacer.pass_period_elapsed(ms(11'952)));
  CHECK(pacer.pass_period_elapsed(ms(11'997)));
}

TEST_CASE(pacer_period_is_measured_from_the_aligned_start) {
  // Lanes with different keyframe offsets must each publish the same span, or
  // the shared period would not actually align them. The first frame the lane
  // sees is the clip's own start; the aligned frame is `offset` later.
  for (const auto offset_ms : {0, 159, 2517}) {
    LanePacer pacer(RunMode::realtime, std::chrono::milliseconds{offset_ms},
                    true, 11'952ms);
    pacer.begin_run(LanePacer::Clock::now());
    const auto clip_origin = 7 * kSecond;
    const auto aligned = clip_origin + ms(offset_ms);
    // Latch the clip origin, then walk to the aligned start.
    CHECK(pacer.past_start_offset(clip_origin) == (offset_ms == 0));
    CHECK(pacer.past_start_offset(aligned));
    pacer.pace(aligned, never_running);
    CHECK(!pacer.pass_period_elapsed(aligned + ms(11'951)));
    CHECK(pacer.pass_period_elapsed(aligned + ms(11'952)));
  }
}

TEST_CASE(pacer_never_wraps_when_looping_is_off_or_no_period_is_set) {
  LanePacer no_loop(RunMode::realtime, 0ms, false, 11'952ms);
  no_loop.begin_run(LanePacer::Clock::now());
  no_loop.pace(0, never_running);
  CHECK(!no_loop.pass_period_elapsed(ms(60'000)));

  // Zero period means "use each file's natural end" — the EOF path handles the
  // wrap, so the period check must stay silent however long the pass runs.
  LanePacer no_period(RunMode::realtime, 0ms, true, 0ms);
  no_period.begin_run(LanePacer::Clock::now());
  no_period.pace(0, never_running);
  CHECK(!no_period.pass_period_elapsed(ms(60'000)));
}

TEST_CASE(pacer_carries_the_cadence_across_a_wrap) {
  // The wall origin advances by exactly one period, so pass 2's frame 0 is due
  // one period after pass 1's. Resetting the clock instead would emit a burst;
  // leaving it put would make every target of pass 2 already overdue.
  LanePacer pacer(RunMode::realtime, 0ms, true, 1'000ms);
  const auto start = LanePacer::Clock::now();
  pacer.begin_run(start);
  pacer.pace(0, never_running);
  pacer.advance_pass();

  // A sleepless pace() on the new pass must not block, and the second frame of
  // the new pass is scheduled relative to the advanced origin.
  const auto before = LanePacer::Clock::now();
  pacer.pace(0, never_running);
  pacer.pace(ms(33), never_running);
  const auto elapsed = LanePacer::Clock::now() - before;
  // is_running() is false throughout, so nothing may actually sleep.
  CHECK(elapsed < 200ms);
}

TEST_CASE(shared_origin_hands_every_later_lane_the_first_lanes_instant) {
  // Lanes start sequentially and open their readers at different speeds. The
  // first to arrive defines media t=0 for the whole run; everyone else adopts it
  // however much later they get there.
  swim::core::SharedLaneOrigin origin;
  const auto first = LanePacer::Clock::now();
  CHECK(origin.latch(first) == first);
  CHECK(origin.latch(first + 400ms) == first);
  CHECK(origin.latch(first - 400ms) == first);
}

TEST_CASE(pacer_late_lane_does_not_anchor_on_its_own_arrival) {
  // The bug this fixes: a lane 400ms slow to produce its first frame used to
  // anchor there and pace every later frame against it, staying 400ms behind
  // for the whole run. On the pool line that was a 13-frame deficit at 1s that
  // was still exactly 13 frames at 20s.
  swim::core::SharedLaneOrigin origin;
  const auto run_start = LanePacer::Clock::now();
  origin.latch(run_start);  // an earlier lane already anchored the run

  LanePacer late(RunMode::realtime, 0ms, false, 0ms, &origin);
  late.begin_run(run_start);
  // This lane's first frame arrives now, well after the run origin. Its second
  // frame is due 33ms after the RUN origin, which is already in the past, so
  // pace() must not sleep — the lane decodes flat out until it is level.
  late.pace(0, [] { return true; });
  const auto before = LanePacer::Clock::now();
  late.pace(ms(33), [] { return true; });
  CHECK(LanePacer::Clock::now() - before < 100ms);
}

TEST_CASE(pacer_without_a_shared_origin_anchors_on_its_own_first_frame) {
  // Single-lane and benchmark callers have nothing to align to, so the default
  // stays "this lane's own arrival defines t=0" and pacing is unchanged.
  LanePacer solo(RunMode::realtime, 0ms, false, 0ms);
  solo.begin_run(LanePacer::Clock::now());
  const auto before = LanePacer::Clock::now();
  solo.pace(0, [] { return true; });
  solo.pace(ms(120), [] { return true; });
  const auto elapsed = LanePacer::Clock::now() - before;
  CHECK(elapsed >= 100ms);
  CHECK(elapsed < 1s);
}

TEST_CASE(shared_origin_gives_every_lane_the_same_origin_per_pass) {
  // Two failure modes this pins down, both measured on 16-lane underwater
  // (2026-07-31, 100s runs, 8 wraps):
  //   * each lane raising its own origin to its own arrival — the lag compounds,
  //     published spread grew to 29 frames (967ms) with no bound;
  //   * nobody raising it — every lane is permanently overdue against a nominal
  //     boundary the run left behind, and the render fell to 29.65fps with the
  //     wrap-time spread at 560ms.
  // The fix: the first lane to reach pass N sets that pass's origin and every
  // later lane adopts it, so the real re-decode cost is absorbed once, equally.
  swim::core::SharedLaneOrigin origin;
  const auto pass0 = LanePacer::Clock::now();
  CHECK(origin.latch_pass(0, pass0) == pass0);
  CHECK(origin.latch_pass(0, pass0 + 300ms) == pass0);

  // Pass 1: the leader's instant wins, and a lane arriving 300ms later adopts it
  // rather than starting its own boundary 300ms downstream.
  const auto pass1 = pass0 + 1200ms;
  CHECK(origin.latch_pass(1, pass1) == pass1);
  CHECK(origin.latch_pass(1, pass1 + 300ms) == pass1);

  // Pass 2 advances again, and a lane still a whole pass behind gets the newest
  // boundary so its targets are overdue and it catches up.
  const auto pass2 = pass1 + 1200ms;
  CHECK(origin.latch_pass(2, pass2) == pass2);
  CHECK(origin.latch_pass(1, pass2 + 50ms) == pass2);
}

TEST_CASE(pacer_late_lane_on_a_wrap_adopts_the_leaders_boundary) {
  // A follower that took 400ms longer over the wrap must pace pass 1 from the
  // leader's boundary, so its 200ms frame is already overdue and it catches up
  // within the pass instead of carrying the lag into pass 2.
  swim::core::SharedLaneOrigin origin;
  const auto run_start = LanePacer::Clock::now() - 3s;
  // The leader reached pass 1 400ms ago.
  origin.latch_pass(0, run_start);
  origin.latch_pass(1, LanePacer::Clock::now() - 400ms);

  LanePacer follower(RunMode::realtime, 0ms, true, 1'000ms, &origin);
  follower.begin_run(run_start);
  follower.pace(0, never_running);
  follower.advance_pass();
  const auto before = LanePacer::Clock::now();
  follower.pace(0, [] { return true; });
  follower.pace(ms(200), [] { return true; });
  CHECK(LanePacer::Clock::now() - before < 150ms);
}

TEST_CASE(pacer_ignores_invalid_timestamps) {
  LanePacer pacer(RunMode::realtime, 500ms, true, 1'000ms);
  pacer.begin_run(LanePacer::Clock::now());
  // "No usable timestamp" must not gate the lane shut, latch an origin, or
  // trigger a wrap — a marker sample would otherwise stall the whole lane.
  CHECK(pacer.past_start_offset(LanePacer::kInvalidPts));
  CHECK(!pacer.pass_period_elapsed(LanePacer::kInvalidPts));
  pacer.pace(LanePacer::kInvalidPts, never_running);
  CHECK(!pacer.past_start_offset(0));
}

TEST_CASE(pacer_does_not_pace_in_benchmark_mode) {
  // Benchmarks measure throughput, so they must run flat out: pace() returns
  // without sleeping and without latching an origin.
  LanePacer pacer(RunMode::benchmark, 0ms, false, 0ms);
  pacer.begin_run(LanePacer::Clock::now());
  const auto before = LanePacer::Clock::now();
  pacer.pace(0, [] { return true; });
  pacer.pace(10 * kSecond, [] { return true; });
  CHECK(LanePacer::Clock::now() - before < 200ms);
}

TEST_CASE(pacer_sleeps_until_the_frames_wall_slot) {
  // The one timing assertion: a frame 120ms into the media timeline must not be
  // published before 120ms of wall time has passed.
  LanePacer pacer(RunMode::realtime, 0ms, false, 0ms);
  pacer.begin_run(LanePacer::Clock::now());
  const auto before = LanePacer::Clock::now();
  pacer.pace(0, [] { return true; });
  pacer.pace(ms(120), [] { return true; });
  const auto elapsed = LanePacer::Clock::now() - before;
  CHECK(elapsed >= 100ms);
  CHECK(elapsed < 1s);
}

TEST_CASE(pacer_pace_returns_when_the_lane_stops) {
  // A stop request must cut a long sleep short instead of holding the lane for
  // the full media interval.
  LanePacer pacer(RunMode::realtime, 0ms, false, 0ms);
  pacer.begin_run(LanePacer::Clock::now());
  pacer.pace(0, [] { return true; });
  const auto before = LanePacer::Clock::now();
  pacer.pace(30 * kSecond, never_running);
  CHECK(LanePacer::Clock::now() - before < 1s);
}
