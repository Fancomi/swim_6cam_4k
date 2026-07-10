#pragma once

#include <swim/core/metrics.hpp>
#include <swim/metal/metal_frame.hpp>

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <stop_token>
#include <utility>

namespace swim::metal {

// Bounded latest-value handoff for exactly one producer and one consumer.
// The producer owns back_, the consumer owns front_, and middle_ transfers a
// slot between them. close_and_clear() requires both sides to be quiescent.
template <typename T>
class PreviewMailbox final {
 private:
  static constexpr std::size_t kSlotCount = 3;
  static constexpr std::uint8_t kDirty = 0x80;
  static constexpr std::uint8_t kIndexMask = 0x03;

  static_assert(kSlotCount - 1 <= kIndexMask);
  static_assert((kDirty & kIndexMask) == 0);

  alignas(64) std::array<T, kSlotCount> slots_{};
  alignas(64) std::atomic<std::uint8_t> middle_{1};
  std::uint8_t back_{2};
  alignas(64) std::uint8_t front_{0};
  alignas(64) std::atomic_bool accepting_{true};
  alignas(64) std::atomic_uint64_t drops_{0};

 public:
  bool offer(T value) noexcept {
    if (!accepting_.load(std::memory_order_acquire)) {
      return false;
    }

    slots_[back_] = std::move(value);
    const auto previous = middle_.exchange(
        static_cast<std::uint8_t>(back_ | kDirty),
        std::memory_order_acq_rel);
    back_ = static_cast<std::uint8_t>(previous & kIndexMask);
    const bool replaced = (previous & kDirty) != 0;
    if (replaced) {
      // The returned dirty slot was pending and is now producer-owned again.
      // Release its lease immediately instead of retaining it until reuse.
      slots_[back_] = T{};
      drops_.fetch_add(1, std::memory_order_relaxed);
    }
    return !replaced;
  }

  bool consume_latest(T& output) noexcept {
    if ((middle_.load(std::memory_order_acquire) & kDirty) == 0) {
      return false;
    }

    const auto previous = middle_.exchange(front_, std::memory_order_acq_rel);
    if ((previous & kDirty) == 0) {
      return false;
    }

    front_ = static_cast<std::uint8_t>(previous & kIndexMask);
    output = std::move(slots_[front_]);
    return true;
  }

  bool accepting() const noexcept {
    return accepting_.load(std::memory_order_acquire);
  }

  bool has_pending() const noexcept {
    return (middle_.load(std::memory_order_acquire) & kDirty) != 0;
  }

  std::uint64_t drops() const noexcept {
    return drops_.load(std::memory_order_relaxed);
  }

  bool close_and_clear() noexcept {
    accepting_.store(false, std::memory_order_release);
    const auto previous = middle_.exchange(front_, std::memory_order_acq_rel);
    const bool discarded = (previous & kDirty) != 0;
    if (discarded) {
      drops_.fetch_add(1, std::memory_order_relaxed);
    }
    for (auto& slot : slots_) {
      slot = T{};
    }
    return discarded;
  }
};

// Exactly-once settlement shared by the bounded timeout path and a possibly
// late native presentation callback. Only one preview command may be in flight.
class PreviewPresentationAccounting final {
 public:
  struct Snapshot final {
    std::uint32_t presents;
    std::uint32_t drops;
    bool pending;
  };

  bool begin() noexcept {
    auto value = value_.load(std::memory_order_acquire);
    for (;;) {
      if ((value & kPending) != 0) {
        return false;
      }
      if (value_.compare_exchange_weak(value, value | kPending,
                                       std::memory_order_acq_rel,
                                       std::memory_order_acquire)) {
        return true;
      }
    }
  }

  bool settle_presented() noexcept { return settle(kPresentIncrement); }
  bool settle_dropped() noexcept { return settle(kDropIncrement); }

  Snapshot snapshot() const noexcept {
    const auto value = value_.load(std::memory_order_acquire);
    return Snapshot{
        static_cast<std::uint32_t>(value & kCountMask),
        static_cast<std::uint32_t>((value >> kDropShift) & kCountMask),
        (value & kPending) != 0,
    };
  }

 private:
  static constexpr std::uint64_t kCountBits = 31;
  static constexpr std::uint64_t kCountMask =
      (std::uint64_t{1} << kCountBits) - 1;
  static constexpr std::uint64_t kDropShift = kCountBits;
  static constexpr std::uint64_t kPresentIncrement = 1;
  static constexpr std::uint64_t kDropIncrement =
      std::uint64_t{1} << kDropShift;
  static constexpr std::uint64_t kPending = std::uint64_t{1} << 62;

  bool settle(std::uint64_t increment) noexcept {
    auto value = value_.load(std::memory_order_acquire);
    for (;;) {
      if ((value & kPending) == 0) {
        return false;
      }
      auto next = value & ~kPending;
      const auto count = increment == kPresentIncrement
                             ? next & kCountMask
                             : (next >> kDropShift) & kCountMask;
      if (count != kCountMask) {
        next += increment;
      }
      if (value_.compare_exchange_weak(value, next,
                                       std::memory_order_acq_rel,
                                       std::memory_order_acquire)) {
        return true;
      }
    }
  }

  std::atomic_uint64_t value_{0};
};

// Main-thread Cocoa/CAMetalLayer presenter. offer() is a non-blocking serial
// producer entry point; all AppKit work remains inside run_main_loop() and
// close_and_drain() on the process main thread.
class MetalPreview final {
 public:
  using CloseCallback = std::function<void()>;

  MetalPreview(std::shared_ptr<MetalContext> context, std::uint32_t width,
               std::uint32_t height, swim::core::RuntimeCounters& metrics,
               CloseCallback close_callback);
  ~MetalPreview();
  MetalPreview(const MetalPreview&) = delete;
  MetalPreview& operator=(const MetalPreview&) = delete;

  bool offer(MetalOutputLease output) noexcept;
  void run_main_loop(std::stop_token token);
  void request_stop() noexcept;
  // Terminal, main-thread operation after the completed-output router flushes.
  void close_and_drain();

 private:
  class Impl;
  std::shared_ptr<Impl> impl_;
};

}  // namespace swim::metal
