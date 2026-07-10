#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <exception>
#include <mutex>

namespace swim::core {

inline void record_atomic_max(std::atomic_uint64_t& target,
                              std::uint64_t value) noexcept {
  auto previous = target.load(std::memory_order_relaxed);
  while (value > previous &&
         !target.compare_exchange_weak(previous, value,
                                       std::memory_order_relaxed,
                                       std::memory_order_relaxed)) {
  }
}

class RenderCompletionGate final {
 public:
  bool try_accept() noexcept {
    std::lock_guard lock(mutex_);
    if (closed_) {
      return false;
    }
    ++pending_;
    return true;
  }

  void complete() noexcept {
    {
      std::lock_guard lock(mutex_);
      if (pending_ == 0) {
        std::terminate();
      }
      --pending_;
    }
    condition_.notify_all();
  }

  bool close_and_wait_until(
      std::chrono::steady_clock::time_point deadline) noexcept {
    std::unique_lock lock(mutex_);
    closed_ = true;
    return condition_.wait_until(lock, deadline,
                                 [this] { return pending_ == 0; });
  }

  std::uint32_t pending() const noexcept {
    std::lock_guard lock(mutex_);
    return pending_;
  }

  bool closed() const noexcept {
    std::lock_guard lock(mutex_);
    return closed_;
  }

 private:
  mutable std::mutex mutex_;
  std::condition_variable condition_;
  std::uint32_t pending_{};
  bool closed_{};
};

}  // namespace swim::core
