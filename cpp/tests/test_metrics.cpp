#include "test_support.hpp"

#include <swim/core/fixed_pool.hpp>
#include <swim/core/hot_path_allocations.hpp>
#include <swim/core/metrics.hpp>

#include <array>
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <cstdint>
#include <exception>
#include <memory>
#include <new>
#include <optional>
#include <type_traits>
#include <thread>
#include <utility>

#if !defined(NDEBUG) && (defined(__unix__) || defined(__APPLE__))
#include <sys/wait.h>
#include <unistd.h>
#endif

using namespace std::chrono_literals;

namespace swim::core::detail {

struct FixedSlotPoolTestAccess {
  template <class T>
  static void release(FixedSlotPool<T>& pool, std::size_t index) noexcept {
    pool.release_slot(index);
  }
};

}  // namespace swim::core::detail

namespace {

struct NonMovableSlot {
  NonMovableSlot() = default;
  NonMovableSlot(const NonMovableSlot&) = delete;
  NonMovableSlot& operator=(const NonMovableSlot&) = delete;
  NonMovableSlot(NonMovableSlot&&) = delete;
  NonMovableSlot& operator=(NonMovableSlot&&) = delete;

  std::uint64_t value{};
};

struct ConcurrentSlot {
  std::atomic_bool in_use{};
};

}  // namespace

TEST_CASE(histogram_reports_fixed_bucket_percentiles) {
  swim::core::FixedLatencyHistogram histogram;
  for (int milliseconds = 1; milliseconds <= 100; ++milliseconds) {
    histogram.observe(std::chrono::milliseconds{milliseconds});
  }

  CHECK_EQ(histogram.percentile(0.50), 50ms);
  CHECK_EQ(histogram.percentile(0.95), 95ms);
  CHECK_EQ(histogram.percentile(0.99), 99ms);
  CHECK_EQ(histogram.count(), 100u);
}

TEST_CASE(histogram_snapshot_clamps_samples_and_resets_source) {
  swim::core::FixedLatencyHistogram histogram;
  histogram.observe(-3ms);
  histogram.observe(10ms);
  histogram.observe(1001ms);
  histogram.observe(99'999ms);

  const auto snapshot = histogram.snapshot_and_reset();
  CHECK_EQ(snapshot.count(), 4u);
  CHECK_EQ(snapshot.percentile(0.0), 0ms);
  CHECK_EQ(snapshot.percentile(-1.0), 0ms);
  CHECK_EQ(snapshot.percentile(0.50), 10ms);
  CHECK_EQ(snapshot.percentile(0.75), 1000ms);
  CHECK_EQ(snapshot.percentile(1.0), 1000ms);
  CHECK_EQ(snapshot.percentile(2.0), 1000ms);

  CHECK_EQ(histogram.count(), 0u);
  CHECK_EQ(histogram.percentile(0.95), 0ms);
  histogram.observe(7ms);
  CHECK_EQ(snapshot.count(), 4u);
  CHECK_EQ(snapshot.percentile(0.50), 10ms);
}

TEST_CASE(runtime_counter_snapshot_is_immutable_and_resets_every_counter) {
  using swim::core::MetricsSnapshot;
  using swim::core::RuntimeCounters;

  static_assert(!std::is_assignable_v<MetricsSnapshot&, MetricsSnapshot>);

  RuntimeCounters counters;
  const auto address = [](const auto& counter) {
    return reinterpret_cast<std::uintptr_t>(&counter);
  };
  CHECK(address(counters.decoded) - address(counters.received) >= 128u);
  CHECK(address(counters.published) - address(counters.decoded) >= 128u);
  CHECK(address(counters.overwritten) - address(counters.published) >= 128u);
  CHECK(address(counters.reused) - address(counters.overwritten) >= 128u);
  CHECK(address(counters.malformed) - address(counters.reused) >= 128u);
  CHECK(address(counters.reconnects) - address(counters.malformed) >= 128u);
  CHECK(address(counters.render_submissions) - address(counters.reconnects) >=
        128u);
  CHECK(address(counters.preview_drops) -
            address(counters.render_submissions) >=
        128u);
  CHECK(address(counters.encode_drops) - address(counters.preview_drops) >=
        128u);
  CHECK(address(counters.decoded_pixel_host_copies) -
            address(counters.encode_drops) >=
        128u);
  CHECK(address(counters.pool_exhaustion) -
            address(counters.decoded_pixel_host_copies) >=
        128u);
  CHECK(address(counters.native_texture_wrappers) -
            address(counters.pool_exhaustion) >=
        128u);
  CHECK(address(counters.native_command_buffers) -
            address(counters.native_texture_wrappers) >=
        128u);
  CHECK(address(counters.native_decode_tickets) -
            address(counters.native_command_buffers) >=
        128u);
  CHECK(address(counters.native_callback_wrappers) -
            address(counters.native_decode_tickets) >=
        128u);
  counters.received.fetch_add(1);
  counters.decoded.fetch_add(2);
  counters.published.fetch_add(3);
  counters.overwritten.fetch_add(4);
  counters.reused.fetch_add(5);
  counters.malformed.fetch_add(6);
  counters.reconnects.fetch_add(7);
  counters.render_submissions.fetch_add(8);
  counters.preview_drops.fetch_add(9);
  counters.encode_drops.fetch_add(10);
  counters.decoded_pixel_host_copies.fetch_add(11);
  counters.pool_exhaustion.fetch_add(12);
  counters.native_texture_wrappers.fetch_add(13);
  counters.native_command_buffers.fetch_add(14);
  counters.native_decode_tickets.fetch_add(15);
  counters.native_callback_wrappers.fetch_add(16);

  const auto snapshot = counters.snapshot_and_reset();
  CHECK_EQ(snapshot.received, 1u);
  CHECK_EQ(snapshot.decoded, 2u);
  CHECK_EQ(snapshot.published, 3u);
  CHECK_EQ(snapshot.overwritten, 4u);
  CHECK_EQ(snapshot.reused, 5u);
  CHECK_EQ(snapshot.malformed, 6u);
  CHECK_EQ(snapshot.reconnects, 7u);
  CHECK_EQ(snapshot.render_submissions, 8u);
  CHECK_EQ(snapshot.preview_drops, 9u);
  CHECK_EQ(snapshot.encode_drops, 10u);
  CHECK_EQ(snapshot.decoded_pixel_host_copies, 11u);
  CHECK_EQ(snapshot.pool_exhaustion, 12u);
  CHECK_EQ(snapshot.native_texture_wrappers, 13u);
  CHECK_EQ(snapshot.native_command_buffers, 14u);
  CHECK_EQ(snapshot.native_decode_tickets, 15u);
  CHECK_EQ(snapshot.native_callback_wrappers, 16u);

  const auto empty = counters.snapshot_and_reset();
  CHECK_EQ(empty.received, 0u);
  CHECK_EQ(empty.decoded, 0u);
  CHECK_EQ(empty.published, 0u);
  CHECK_EQ(empty.overwritten, 0u);
  CHECK_EQ(empty.reused, 0u);
  CHECK_EQ(empty.malformed, 0u);
  CHECK_EQ(empty.reconnects, 0u);
  CHECK_EQ(empty.render_submissions, 0u);
  CHECK_EQ(empty.preview_drops, 0u);
  CHECK_EQ(empty.encode_drops, 0u);
  CHECK_EQ(empty.decoded_pixel_host_copies, 0u);
  CHECK_EQ(empty.pool_exhaustion, 0u);
  CHECK_EQ(empty.native_texture_wrappers, 0u);
  CHECK_EQ(empty.native_command_buffers, 0u);
  CHECK_EQ(empty.native_decode_tickets, 0u);
  CHECK_EQ(empty.native_callback_wrappers, 0u);
}

TEST_CASE(fixed_pool_rejects_capacities_outside_one_to_sixty_four) {
  CHECK_THROWS_WITH((swim::core::FixedSlotPool<std::uint64_t>{0}),
                    "fixed pool capacity must be between 1 and 64");
  CHECK_THROWS_WITH((swim::core::FixedSlotPool<std::uint64_t>{65}),
                    "fixed pool capacity must be between 1 and 64");
}

TEST_CASE(fixed_pool_exhaustion_reuses_the_released_stable_slot) {
  swim::core::FixedSlotPool<NonMovableSlot> pool{2};
  auto first = pool.try_acquire();
  auto second = pool.try_acquire();

  CHECK(first.has_value());
  CHECK(second.has_value());
  CHECK(!pool.try_acquire().has_value());
  CHECK_EQ(pool.capacity(), 2u);

  first->operator->()->value = 41;
  second->operator->()->value = 42;
  auto* const released_address = second->operator->();
  const auto released_index = second->index();
  second.reset();

  auto reused = pool.try_acquire();
  CHECK(reused.has_value());
  CHECK_EQ(reused->index(), released_index);
  CHECK_EQ(reused->operator->(), released_address);
  CHECK_EQ(reused->operator->()->value, 42u);
}

TEST_CASE(fixed_pool_moved_from_lease_does_not_release_its_slot) {
  swim::core::FixedSlotPool<std::uint64_t> pool{1};
  auto original = pool.try_acquire();
  auto moved = std::move(original);

  original.reset();
  CHECK(!pool.try_acquire().has_value());
  moved.reset();
  CHECK(pool.try_acquire().has_value());
}

TEST_CASE(fixed_pool_debug_guard_terminates_on_double_release) {
#if !defined(NDEBUG) && (defined(__unix__) || defined(__APPLE__))
  const auto child = fork();
  CHECK(child >= 0);
  if (child == 0) {
    std::set_terminate([] { std::_Exit(86); });
    swim::core::FixedSlotPool<std::uint64_t> pool{1};
    auto lease = pool.try_acquire();
    swim::core::detail::FixedSlotPoolTestAccess::release(pool, lease->index());
    lease.reset();
    std::_Exit(0);
  }

  int status = 0;
  CHECK_EQ(waitpid(child, &status, 0), child);
  CHECK(WIFEXITED(status));
  CHECK_EQ(WEXITSTATUS(status), 86);
#endif
}

TEST_CASE(fixed_pool_uses_all_sixty_four_free_mask_bits) {
  using Pool = swim::core::FixedSlotPool<std::uint64_t>;
  Pool pool{64};
  std::array<std::optional<Pool::Lease>, 64> leases;

  for (std::size_t index = 0; index < leases.size(); ++index) {
    leases[index] = pool.try_acquire();
    CHECK(leases[index].has_value());
    CHECK_EQ(leases[index]->index(), index);
  }
  CHECK(!pool.try_acquire().has_value());

  leases.back().reset();
  auto high_bit = pool.try_acquire();
  CHECK(high_bit.has_value());
  CHECK_EQ(high_bit->index(), 63u);
}

TEST_CASE(fixed_pool_atomic_mask_never_grants_one_slot_twice) {
  swim::core::FixedSlotPool<ConcurrentSlot> pool{2};
  std::atomic_uint collisions{0};
  std::atomic_uint acquisitions{0};
  std::array<std::jthread, 4> workers;

  for (auto& worker : workers) {
    worker = std::jthread([&] {
      for (std::size_t iteration = 0; iteration < 50'000; ++iteration) {
        for (;;) {
          auto lease = pool.try_acquire();
          if (!lease.has_value()) {
            std::this_thread::yield();
            continue;
          }
          if ((*lease)->in_use.exchange(true, std::memory_order_acq_rel)) {
            collisions.fetch_add(1, std::memory_order_relaxed);
          }
          (*lease)->in_use.store(false, std::memory_order_release);
          acquisitions.fetch_add(1, std::memory_order_relaxed);
          break;
        }
      }
    });
  }
  for (auto& worker : workers) {
    worker.join();
  }

  CHECK_EQ(collisions.load(std::memory_order_relaxed), 0u);
  CHECK_EQ(acquisitions.load(std::memory_order_relaxed), 200'000u);
}

TEST_CASE(global_new_forms_count_only_inside_hot_path_scope) {
  using swim::core::HotPathAllocationScope;
  using swim::core::hot_path_allocation_count;

  auto* outside = ::operator new(8);
  ::operator delete(outside);
  const auto before = hot_path_allocation_count();

  {
    HotPathAllocationScope scope;
    auto* scalar = ::operator new(8);
    ::operator delete(scalar);
    auto* array = ::operator new[](16);
    ::operator delete[](array);
    auto* aligned_scalar = ::operator new(64, std::align_val_t{64});
    CHECK_EQ(reinterpret_cast<std::uintptr_t>(aligned_scalar) % 64u, 0u);
    ::operator delete(aligned_scalar, std::align_val_t{64});
    auto* aligned_array = ::operator new[](128, std::align_val_t{64});
    CHECK_EQ(reinterpret_cast<std::uintptr_t>(aligned_array) % 64u, 0u);
    ::operator delete[](aligned_array, std::align_val_t{64});
  }

  CHECK_EQ(hot_path_allocation_count(), before + 4u);
  outside = ::operator new(8);
  ::operator delete(outside);
  CHECK_EQ(hot_path_allocation_count(), before + 4u);
}

TEST_CASE(nested_hot_path_scopes_keep_the_outer_scope_active) {
  using swim::core::HotPathAllocationScope;
  using swim::core::hot_path_allocation_count;

  const auto before = hot_path_allocation_count();
  {
    HotPathAllocationScope outer;
    auto* first = ::operator new(8);
    ::operator delete(first);
    {
      HotPathAllocationScope inner;
      auto* second = ::operator new[](8);
      ::operator delete[](second);
    }
    auto* third = ::operator new(8, std::align_val_t{64});
    ::operator delete(third, std::align_val_t{64});
  }
  CHECK_EQ(hot_path_allocation_count(), before + 3u);
}

TEST_CASE(hot_path_scope_is_local_to_the_calling_thread) {
  std::atomic_bool allocate{false};
  std::atomic_bool allocated{false};
  std::jthread other_thread([&] {
    while (!allocate.load(std::memory_order_acquire)) {
      std::this_thread::yield();
    }
    auto* memory = ::operator new(8);
    ::operator delete(memory);
    allocated.store(true, std::memory_order_release);
  });
  const auto before = swim::core::hot_path_allocation_count();

  {
    swim::core::HotPathAllocationScope scope;
    allocate.store(true, std::memory_order_release);
    while (!allocated.load(std::memory_order_acquire)) {
      std::this_thread::yield();
    }
  }
  other_thread.join();

  CHECK_EQ(swim::core::hot_path_allocation_count(), before);
}

TEST_CASE(c_allocation_is_not_classified_as_application_cpp_new) {
  const auto before = swim::core::hot_path_allocation_count();
  {
    swim::core::HotPathAllocationScope scope;
    auto* memory = std::malloc(8);
    CHECK(memory != nullptr);
    std::free(memory);
  }
  CHECK_EQ(swim::core::hot_path_allocation_count(), before);
}

TEST_CASE(global_delete_forms_match_all_replaced_allocation_forms) {
  auto* sized_scalar = ::operator new(17);
  ::operator delete(sized_scalar, std::size_t{17});
  auto* sized_array = ::operator new[](19);
  ::operator delete[](sized_array, std::size_t{19});
  auto* sized_aligned = ::operator new(64, std::align_val_t{64});
  ::operator delete(sized_aligned, std::size_t{64}, std::align_val_t{64});
  auto* sized_aligned_array =
      ::operator new[](128, std::align_val_t{64});
  ::operator delete[](sized_aligned_array, std::size_t{128},
                      std::align_val_t{64});

  auto* nothrow_scalar = ::operator new(23, std::nothrow);
  CHECK(nothrow_scalar != nullptr);
  ::operator delete(nothrow_scalar, std::nothrow);
  auto* nothrow_aligned =
      ::operator new[](64, std::align_val_t{64}, std::nothrow);
  CHECK(nothrow_aligned != nullptr);
  ::operator delete[](nothrow_aligned, std::align_val_t{64}, std::nothrow);
}

TEST_CASE(fixed_pool_operations_do_not_allocate_after_construction) {
  swim::core::FixedSlotPool<std::uint64_t> pool{4};
  const auto before = swim::core::hot_path_allocation_count();

  {
    swim::core::HotPathAllocationScope scope;
    for (std::size_t iteration = 0; iteration < 1000; ++iteration) {
      auto lease = pool.try_acquire();
      CHECK(lease.has_value());
      **lease = iteration;
    }
  }

  CHECK_EQ(swim::core::hot_path_allocation_count(), before);
}
