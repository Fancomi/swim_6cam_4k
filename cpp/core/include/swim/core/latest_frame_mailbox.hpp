#pragma once

#include <swim/core/frame.hpp>

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <utility>

namespace swim::core {

// Wait-free latest-value handoff for exactly one producer and one consumer.
// Calling publish from multiple threads, or consume_latest from multiple
// threads, violates the ownership protocol for back_ and front_.
class LatestFrameMailbox {
 private:
  static constexpr std::size_t kSlotCount = 3;
  static constexpr std::uint8_t kDirty = 0x80;
  static constexpr std::uint8_t kIndexMask = 0x03;

  static_assert(kSlotCount > 0);
  static_assert(kSlotCount - 1 <= kIndexMask,
                "mailbox slot indices must fit the index mask");
  static_assert((kDirty & kIndexMask) == 0,
                "dirty flag must not overlap mailbox slot indices");

  alignas(64) std::array<FrameLease, kSlotCount> slots_;
  alignas(64) std::atomic<std::uint8_t> middle_{1};
  std::uint8_t back_{2};
  alignas(64) std::uint8_t front_{0};

 public:
  void publish(FrameLease frame) noexcept {
    slots_[back_] = std::move(frame);
    const auto previous = middle_.exchange(
        static_cast<std::uint8_t>(back_ | kDirty),
        std::memory_order_acq_rel);
    back_ = static_cast<std::uint8_t>(previous & kIndexMask);
  }

  bool consume_latest(FrameLease& output) {
    if ((middle_.load(std::memory_order_acquire) & kDirty) == 0) {
      return false;
    }

    const auto previous =
        middle_.exchange(front_, std::memory_order_acq_rel);
    if ((previous & kDirty) == 0) {
      return false;
    }

    front_ = static_cast<std::uint8_t>(previous & kIndexMask);
    output = slots_[front_];
    return true;
  }
};

}  // namespace swim::core
