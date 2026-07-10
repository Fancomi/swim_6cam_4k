#include "test_support.hpp"

#include <swim/core/hot_path_allocations.hpp>
#include <swim/metal/metal_preview.hpp>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <optional>
#include <utility>

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

  mailbox.close_and_clear();
  CHECK(!mailbox.accepting());
  CHECK(!mailbox.has_pending());
  CHECK_EQ(pending.references.load(std::memory_order_relaxed), 0u);
  CHECK(!mailbox.offer(TrackedLease{rejected}));
  CHECK_EQ(rejected.references.load(std::memory_order_relaxed), 0u);
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
