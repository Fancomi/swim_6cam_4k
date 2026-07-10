#pragma once

#include <atomic>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <memory>
#include <optional>
#include <stdexcept>
#include <utility>

namespace swim::core {

namespace detail {
struct FixedSlotPoolTestAccess;
}  // namespace detail

template <class T>
class FixedSlotPool final {
 public:
  // Lifetime contract: this pool must outlive every Lease, and destruction
  // must not race any pool operation. Destruction with an owned slot rejects
  // the contract violation by terminating before the slots or mask are
  // destroyed, in every build type.
  class Lease final {
   public:
    Lease(const Lease&) = delete;
    Lease& operator=(const Lease&) = delete;

    Lease(Lease&& other) noexcept
        : pool_(std::exchange(other.pool_, nullptr)), index_(other.index_) {}

    Lease& operator=(Lease&& other) noexcept {
      if (this == &other) {
        return *this;
      }
      release();
      pool_ = std::exchange(other.pool_, nullptr);
      index_ = other.index_;
      return *this;
    }

    ~Lease() { release(); }

    T& operator*() const noexcept { return pool_->slots_[index_]; }
    T* operator->() const noexcept { return &pool_->slots_[index_]; }
    std::size_t index() const noexcept { return index_; }

   private:
    friend class FixedSlotPool;

    Lease(FixedSlotPool* pool, std::size_t index) noexcept
        : pool_(pool), index_(index) {}

    void release() noexcept {
      if (pool_ != nullptr) {
        pool_->release_slot(index_);
        pool_ = nullptr;
      }
    }

    FixedSlotPool* pool_{};
    std::size_t index_{};
  };

  explicit FixedSlotPool(std::size_t capacity)
      : capacity_(capacity),
        slots_(allocate_slots(capacity)),
        free_mask_(initial_free_mask(capacity)) {}

  ~FixedSlotPool() noexcept {
    if (free_mask_.load(std::memory_order_acquire) !=
        initial_free_mask(capacity_)) {
      std::terminate();
    }
  }

  FixedSlotPool(const FixedSlotPool&) = delete;
  FixedSlotPool& operator=(const FixedSlotPool&) = delete;
  FixedSlotPool(FixedSlotPool&&) = delete;
  FixedSlotPool& operator=(FixedSlotPool&&) = delete;

  std::optional<Lease> try_acquire() noexcept {
    auto available = free_mask_.load(std::memory_order_relaxed);
    while (available != 0) {
      const auto index =
          static_cast<std::size_t>(std::countr_zero(available));
      const auto bit = std::uint64_t{1} << index;
      const auto remaining = available & ~bit;
      if (free_mask_.compare_exchange_weak(
              available, remaining, std::memory_order_acquire,
              std::memory_order_relaxed)) {
        Lease lease{this, index};
        return std::optional<Lease>{std::move(lease)};
      }
    }
    return std::nullopt;
  }

  std::size_t capacity() const noexcept { return capacity_; }

 private:
  friend struct detail::FixedSlotPoolTestAccess;

  static std::unique_ptr<T[]> allocate_slots(std::size_t capacity) {
    if (capacity == 0 || capacity > 64) {
      throw std::invalid_argument(
          "fixed pool capacity must be between 1 and 64");
    }
    return std::make_unique<T[]>(capacity);
  }

  static std::uint64_t initial_free_mask(std::size_t capacity) noexcept {
    if (capacity == 64) {
      return ~std::uint64_t{0};
    }
    return (std::uint64_t{1} << capacity) - 1;
  }

  void release_slot(std::size_t index) noexcept {
    const auto bit = std::uint64_t{1} << index;
    const auto previous = free_mask_.fetch_or(bit, std::memory_order_release);
#ifndef NDEBUG
    if ((previous & bit) != 0) {
      std::terminate();
    }
#else
    static_cast<void>(previous);
#endif
  }

  const std::size_t capacity_;
  const std::unique_ptr<T[]> slots_;
  std::atomic_uint64_t free_mask_;
};

static_assert(std::atomic_uint64_t::is_always_lock_free);

}  // namespace swim::core
