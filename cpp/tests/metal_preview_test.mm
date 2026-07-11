#include "test_support.hpp"

#include <swim/core/hot_path_allocations.hpp>
#include <swim/metal/metal_preview.hpp>

#include <atomic>
#include <barrier>
#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>
#include <thread>
#include <utility>

#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>

namespace {
using namespace std::chrono_literals;

struct LeaseState final {
  std::atomic_uint32_t references{0};
};

class TrackedLease final {
 public:
  TrackedLease() = default;
  explicit TrackedLease(LeaseState& state) noexcept : state_(&state) {
    state_->references.fetch_add(1, std::memory_order_relaxed);
  }
  TrackedLease(const TrackedLease& other) noexcept : state_(other.state_) {
    retain();
  }
  TrackedLease& operator=(const TrackedLease& other) noexcept {
    if (this == &other) {
      return *this;
    }
    reset();
    state_ = other.state_;
    retain();
    return *this;
  }
  TrackedLease(TrackedLease&& other) noexcept
      : state_(std::exchange(other.state_, nullptr)) {}
  TrackedLease& operator=(TrackedLease&& other) noexcept {
    if (this != &other) {
      reset();
      state_ = std::exchange(other.state_, nullptr);
    }
    return *this;
  }
  ~TrackedLease() { reset(); }

 private:
  void retain() noexcept {
    if (state_ != nullptr) {
      state_->references.fetch_add(1, std::memory_order_relaxed);
    }
  }
  void reset() noexcept {
    if (auto* state = std::exchange(state_, nullptr); state != nullptr) {
      state->references.fetch_sub(1, std::memory_order_relaxed);
    }
  }

  LeaseState* state_{};
};

class FakePresenter final {
 public:
  void present(TrackedLease lease) { in_flight_.emplace(std::move(lease)); }
  void complete() { in_flight_.reset(); }

 private:
  std::optional<TrackedLease> in_flight_;
};
}  // namespace

TEST_CASE(offscreen_preview_executes_real_gpu_copy_without_appkit_window) {
  auto context = std::make_shared<swim::metal::MetalContext>();
  context->device = MTLCreateSystemDefaultDevice();
  CHECK(context->device != nil);
  context->command_queue = [context->device newCommandQueue];
  CHECK(context->command_queue != nil);
  CHECK_EQ(CVMetalTextureCacheCreate(kCFAllocatorDefault, nullptr,
                                    context->device, nullptr,
                                    &context->texture_cache),
           kCVReturnSuccess);
  swim::core::RuntimeCounters counters;
  auto pool = std::make_shared<swim::metal::MetalOutputPool>(
      context, 1, 64, 32);
  auto lease = pool->try_acquire();
  CHECK(lease.has_value());
  if (!lease.has_value()) {
    return;
  }

  swim::metal::MetalPreview preview{context, 64, 32, counters, [] {}, false};
  CHECK(preview.offer(std::move(*lease)));
  preview.close_and_drain();

  const auto totals = counters.sample_totals();
  CHECK_EQ(totals.preview_submissions, 1u);
  CHECK_EQ(totals.preview_completions, 1u);
  CHECK_EQ(totals.preview_presents, 1u);
  CHECK_EQ(totals.preview_drops, 0u);
  CHECK_EQ(pool->in_use(), 0u);
}

TEST_CASE(offscreen_terminal_close_wakes_a_concurrent_main_loop_waiter) {
  auto context = std::make_shared<swim::metal::MetalContext>();
  context->device = MTLCreateSystemDefaultDevice();
  CHECK(context->device != nil);
  context->command_queue = [context->device newCommandQueue];
  CHECK(context->command_queue != nil);
  swim::core::RuntimeCounters counters;
  swim::metal::MetalPreview preview{context, 64, 32, counters, [] {}, false};
  std::atomic_bool entered{false};
  std::atomic_bool exited{false};
  std::jthread loop([&](std::stop_token token) {
    entered.store(true, std::memory_order_release);
    preview.run_main_loop(token);
    exited.store(true, std::memory_order_release);
  });
  while (!entered.load(std::memory_order_acquire)) {
    std::this_thread::yield();
  }
  std::this_thread::sleep_for(20ms);

  preview.close_and_drain();
  const auto deadline = std::chrono::steady_clock::now() + 100ms;
  while (!exited.load(std::memory_order_acquire) &&
         std::chrono::steady_clock::now() < deadline) {
    std::this_thread::yield();
  }
  const bool close_woke_loop = exited.load(std::memory_order_acquire);
  if (!close_woke_loop) {
    preview.request_stop();
  }
  loop.join();
  CHECK(close_woke_loop);
}

TEST_CASE(preview_offer_never_blocks_when_capacity_one_is_full) {
  swim::metal::PreviewMailbox<std::uint64_t> mailbox;
  CHECK(mailbox.offer(1));
  const auto started = std::chrono::steady_clock::now();
  CHECK(!mailbox.offer(2));
  CHECK(std::chrono::steady_clock::now() - started < 1ms);
  CHECK_EQ(mailbox.drops(), 1u);

  std::uint64_t latest = 0;
  CHECK(mailbox.consume_latest(latest));
  CHECK_EQ(latest, 2u);
  CHECK(!mailbox.consume_latest(latest));
}

TEST_CASE(preview_replacement_releases_the_unconsumed_lease_immediately) {
  swim::metal::PreviewMailbox<TrackedLease> mailbox;
  LeaseState replaced;
  LeaseState latest;

  CHECK(mailbox.offer(TrackedLease{replaced}));
  CHECK_EQ(replaced.references.load(std::memory_order_relaxed), 1u);
  CHECK(!mailbox.offer(TrackedLease{latest}));
  CHECK_EQ(replaced.references.load(std::memory_order_relaxed), 0u);
  CHECK_EQ(latest.references.load(std::memory_order_relaxed), 1u);
}

TEST_CASE(preview_presenter_retains_the_lease_until_gpu_completion) {
  swim::metal::PreviewMailbox<TrackedLease> mailbox;
  LeaseState state;
  CHECK(mailbox.offer(TrackedLease{state}));

  TrackedLease lease;
  CHECK(mailbox.consume_latest(lease));
  FakePresenter presenter;
  presenter.present(std::move(lease));
  CHECK_EQ(state.references.load(std::memory_order_relaxed), 1u);
  presenter.complete();
  CHECK_EQ(state.references.load(std::memory_order_relaxed), 0u);
}

TEST_CASE(preview_close_rejects_offers_and_releases_pending_leases) {
  swim::metal::PreviewMailbox<TrackedLease> mailbox;
  LeaseState pending;
  LeaseState rejected;
  CHECK(mailbox.offer(TrackedLease{pending}));

  CHECK(mailbox.close_and_clear());
  CHECK(!mailbox.accepting());
  CHECK(!mailbox.has_pending());
  CHECK_EQ(mailbox.drops(), 1u);
  CHECK_EQ(pending.references.load(std::memory_order_relaxed), 0u);
  CHECK(!mailbox.offer(TrackedLease{rejected}));
  CHECK_EQ(rejected.references.load(std::memory_order_relaxed), 0u);
}

TEST_CASE(preview_close_without_a_pending_value_reports_no_discard) {
  swim::metal::PreviewMailbox<std::uint64_t> mailbox;
  CHECK(!mailbox.close_and_clear());
  CHECK_EQ(mailbox.drops(), 0u);
}

TEST_CASE(preview_timeout_and_late_callback_settle_one_presentation_once) {
  swim::metal::PreviewPresentationAccounting accounting;
  CHECK(accounting.begin());
  CHECK(accounting.settle_dropped());
  CHECK(!accounting.settle_presented());
  auto snapshot = accounting.snapshot();
  CHECK_EQ(snapshot.presents, 0u);
  CHECK_EQ(snapshot.drops, 1u);
  CHECK(!snapshot.pending);

  CHECK(accounting.begin());
  CHECK(accounting.settle_presented());
  CHECK(!accounting.settle_dropped());
  snapshot = accounting.snapshot();
  CHECK_EQ(snapshot.presents, 1u);
  CHECK_EQ(snapshot.drops, 1u);
  CHECK(!snapshot.pending);
}

TEST_CASE(preview_timeout_and_callback_publish_one_atomic_disposition) {
  swim::metal::PreviewPresentationAccounting accounting;
  for (std::uint32_t iteration = 0; iteration < 1000; ++iteration) {
    CHECK(accounting.begin());
    std::barrier start{3};
    bool presented = false;
    bool dropped = false;
    std::jthread callback([&] {
      start.arrive_and_wait();
      presented = accounting.settle_presented();
    });
    std::jthread timeout([&] {
      start.arrive_and_wait();
      dropped = accounting.settle_dropped();
    });
    start.arrive_and_wait();
    callback.join();
    timeout.join();
    CHECK(presented != dropped);
    const auto snapshot = accounting.snapshot();
    CHECK_EQ(snapshot.presents + snapshot.drops,
             static_cast<std::uint64_t>(iteration) + 1u);
    CHECK(!snapshot.pending);
  }
}

TEST_CASE(preview_mailbox_offer_allocates_no_application_heap_memory) {
  swim::metal::PreviewMailbox<std::uint64_t> mailbox;
  const auto before = swim::core::hot_path_allocation_count();
  {
    swim::core::HotPathAllocationScope hot_path;
    CHECK(mailbox.offer(42));
  }
  CHECK_EQ(swim::core::hot_path_allocation_count(), before);
}

TEST_CASE(completed_output_lease_copies_keep_the_pool_owner_anchored) {
  auto owner = std::make_shared<std::uint64_t>(42);
  std::weak_ptr<std::uint64_t> weak_owner = owner;
  swim::metal::MetalOutputLease routed;
  routed.anchor_lifetime(owner);
  auto downstream = routed;
  auto presented = std::move(downstream);

  owner.reset();
  routed = {};
  CHECK(!weak_owner.expired());
  CHECK(!downstream);
  presented = {};
  CHECK(weak_owner.expired());
}
