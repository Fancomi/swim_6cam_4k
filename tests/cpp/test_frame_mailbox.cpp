#include "test_support.hpp"

#include <swim/core/frame.hpp>
#include <swim/core/latest_frame_mailbox.hpp>

#include <atomic>
#include <cstdint>
#include <thread>
#include <utility>

namespace {

constexpr std::uint32_t kTestBackendTag = 0x54455354;

struct MockNativeStats {
  std::atomic_int retains{0};
  std::atomic_int releases{0};
  std::atomic_int live_objects{0};
};

struct MockNative {
  explicit MockNative(MockNativeStats& owner) noexcept : stats(&owner) {
    stats->retains.fetch_add(1, std::memory_order_relaxed);
    stats->live_objects.fetch_add(1, std::memory_order_relaxed);
  }

  MockNativeStats* stats;
  std::atomic_int references{1};
};

void mock_retain(void* pointer) noexcept {
  auto& native = *static_cast<MockNative*>(pointer);
  native.references.fetch_add(1, std::memory_order_relaxed);
  native.stats->retains.fetch_add(1, std::memory_order_relaxed);
}

void mock_release(void* pointer) noexcept {
  auto* native = static_cast<MockNative*>(pointer);
  auto* stats = native->stats;
  stats->releases.fetch_add(1, std::memory_order_relaxed);
  if (native->references.fetch_sub(1, std::memory_order_acq_rel) == 1) {
    stats->live_objects.fetch_sub(1, std::memory_order_relaxed);
    delete native;
  }
}

swim::core::FrameLease mock_frame(MockNativeStats& stats,
                                  std::uint64_t sequence) {
  swim::core::FrameMetadata metadata{};
  metadata.sequence = sequence;

  // The new native object's initial reference is transferred to FrameLease.
  // Its counters outlive the native allocation, so balance checks never read
  // an object after the final release has destroyed it.
  auto* native = new MockNative(stats);
  return swim::core::FrameLease{
      native,
      swim::core::NativeLeaseOps{mock_retain, mock_release, kTestBackendTag},
      metadata};
}

void check_balanced(const MockNativeStats& stats) {
  CHECK_EQ(stats.live_objects.load(std::memory_order_relaxed), 0);
  CHECK_EQ(stats.retains.load(std::memory_order_relaxed),
           stats.releases.load(std::memory_order_relaxed));
}

}  // namespace

TEST_CASE(mailbox_returns_latest_complete_generation) {
  MockNativeStats stats;
  {
    swim::core::LatestFrameMailbox box;
    box.publish(mock_frame(stats, 1));
    box.publish(mock_frame(stats, 2));
    box.publish(mock_frame(stats, 3));

    swim::core::FrameLease frame;
    CHECK(box.consume_latest(frame));
    CHECK_EQ(frame.metadata().sequence, 3u);
    CHECK(!box.consume_latest(frame));
  }
  check_balanced(stats);
}

TEST_CASE(inflight_copy_retains_native_surface) {
  MockNativeStats stats;
  {
    auto front = mock_frame(stats, 7);
    auto inflight = front;
    CHECK_EQ(stats.retains.load(std::memory_order_relaxed), 2);
    CHECK_EQ(inflight.metadata().sequence, 7u);
  }
  CHECK_EQ(stats.releases.load(std::memory_order_relaxed), 2);
  check_balanced(stats);
}

TEST_CASE(frame_lease_moves_and_checks_backend_identity) {
  MockNativeStats stats;
  {
    auto source = mock_frame(stats, 9);
    auto* expected_native = source.native(kTestBackendTag);
    swim::core::FrameLease destination = std::move(source);

    CHECK(!source);
    CHECK(destination);
    CHECK_EQ(destination.native(kTestBackendTag), expected_native);
    CHECK_THROWS_WITH(destination.native(0x42414421),
                      "frame backend tag mismatch");
  }
  check_balanced(stats);
}

TEST_CASE(mixed_rate_stress_never_regresses) {
  MockNativeStats stats;
  {
    swim::core::LatestFrameMailbox box;
    std::atomic_bool done{false};
    std::jthread producer([&] {
      for (std::uint64_t sequence = 1; sequence <= 2'000'000; ++sequence) {
        box.publish(mock_frame(stats, sequence));
      }
      done.store(true, std::memory_order_release);
    });

    std::uint64_t previous = 0;
    swim::core::FrameLease frame;
    while (!done.load(std::memory_order_acquire)) {
      if (box.consume_latest(frame)) {
        CHECK(frame.metadata().sequence > previous);
        previous = frame.metadata().sequence;
      }
    }
    while (box.consume_latest(frame)) {
      CHECK(frame.metadata().sequence > previous);
      previous = frame.metadata().sequence;
    }

    producer.join();
    CHECK_EQ(previous, 2'000'000u);
  }
  check_balanced(stats);
}
