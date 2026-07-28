#include "test_support.hpp"

#include <swim/core/render_completion_gate.hpp>
#include <swim/core/run_lifecycle.hpp>

#include <array>
#include <atomic>
#include <chrono>
#include <thread>

namespace {
using namespace std::chrono_literals;
}

TEST_CASE(run_lifecycle_classifies_every_eof_boundary) {
  using swim::core::SourceEofDisposition;
  const auto t0 = swim::core::RunLifecycle::Clock::now();
  swim::core::RunLifecycle finite{10s};
  CHECK_EQ(finite.classify_eof(t0),
           SourceEofDisposition::fatal_before_active);
  CHECK_EQ(std::string{swim::core::source_eof_failure_message(
               finite.classify_eof(t0))},
           "MP4 reached EOF before global render became active");
  CHECK(finite.mark_active(t0));
  CHECK_EQ(finite.deadline(), t0 + 10s);
  CHECK_EQ(finite.classify_eof(t0 + 9999ms),
           SourceEofDisposition::fatal_before_deadline);
  CHECK_EQ(finite.classify_eof(t0 + 10s),
           SourceEofDisposition::normal_after_deadline);

  swim::core::RunLifecycle unbounded{0s};
  CHECK(unbounded.mark_active(t0));
  CHECK_EQ(unbounded.classify_eof(t0 + 1h),
           SourceEofDisposition::fatal_unbounded);
  unbounded.request_stop();
  CHECK_EQ(unbounded.classify_eof(t0 + 1h),
           SourceEofDisposition::normal_after_stop);
}

TEST_CASE(run_lifecycle_first_activation_owns_the_global_deadline) {
  const auto t0 = swim::core::RunLifecycle::Clock::now();
  swim::core::RunLifecycle lifecycle{3s};
  CHECK(lifecycle.mark_active(t0));
  CHECK(!lifecycle.mark_active(t0 + 1s));
  CHECK_EQ(lifecycle.deadline(), t0 + 3s);
}

TEST_CASE(runtime_start_state_counts_only_completed_healthy_starts) {
  swim::core::RuntimeStartState state;
  std::array<bool, swim::core::kMaxCameras> failed{};
  CHECK_EQ(state.healthy_count(failed), 0u);
  state.mark_started(0);
  state.mark_started(2);
  CHECK_EQ(state.healthy_count(failed), 2u);
  failed[2] = true;
  CHECK_EQ(state.healthy_count(failed), 1u);
}

TEST_CASE(render_completion_gate_notifies_and_times_out_boundedly) {
  swim::core::RenderCompletionGate gate;
  CHECK(gate.try_accept());
  CHECK(!gate.close_and_wait_until(std::chrono::steady_clock::now()));
  CHECK(!gate.try_accept());
  std::jthread completion([&] {
    std::this_thread::sleep_for(2ms);
    gate.complete();
  });
  CHECK(gate.close_and_wait_until(std::chrono::steady_clock::now() + 100ms));
  completion.join();
  CHECK_EQ(gate.pending(), 0u);
}

TEST_CASE(atomic_max_never_regresses_on_out_of_order_completion) {
  std::atomic_uint64_t value{100};
  swim::core::record_atomic_max(value, 300);
  swim::core::record_atomic_max(value, 200);
  CHECK_EQ(value.load(), 300u);
}
