#pragma once

#include <swim/core/config.hpp>
#include <swim/core/fixed_pool.hpp>
#include <swim/core/metrics.hpp>
#include <swim/metal/metal_frame.hpp>

#import <CoreMedia/CoreMedia.h>

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <atomic>
#include <memory>
#include <mutex>
#include <optional>
#include <span>
#include <string>

namespace swim::metal {

struct AnnexBWriter final {
  void* context{};
  bool (*append)(void*, std::span<const std::uint8_t>) noexcept{};
};

bool write_length_prefixed_nals_as_annex_b(
    std::span<const std::uint8_t> access_unit,
    std::uint8_t nal_length_bytes,
    AnnexBWriter writer) noexcept;

bool write_length_prefixed_nals_as_annex_b(
    CMBlockBufferRef access_unit,
    std::size_t access_unit_bytes,
    std::uint8_t nal_length_bytes,
    AnnexBWriter writer) noexcept;

bool write_hevc_access_unit_as_annex_b(
    std::span<const std::uint8_t> access_unit,
    std::uint8_t nal_length_bytes,
    std::span<const std::span<const std::uint8_t>> parameter_sets,
    AnnexBWriter writer) noexcept;

struct EncoderInputRecord final {
  MetalOutputLease output;
  CMTime pts{kCMTimeInvalid};
  std::uint64_t submission_sequence{};
};

class EncoderInputGate final {
 private:
  using Pool = swim::core::FixedSlotPool<EncoderInputRecord>;
  using PoolLease = Pool::Lease;

 public:
  class Lease final {
   public:
    Lease(const Lease&) = delete;
    Lease& operator=(const Lease&) = delete;
    Lease(Lease&& other) noexcept;
    Lease& operator=(Lease&& other) noexcept;
    ~Lease();

    EncoderInputRecord* operator->() const noexcept;
    EncoderInputRecord& operator*() const noexcept;
    std::size_t index() const noexcept;

   private:
    friend class EncoderInputGate;
    Lease(EncoderInputGate* owner, PoolLease lease) noexcept;
    void release() noexcept;
    EncoderInputGate* owner_{};
    std::optional<PoolLease> lease_;
  };

  struct Ticket final {
   private:
    friend class EncoderInputGate;
    EncoderInputGate* owner{};
    std::optional<PoolLease> lease;
  };

  explicit EncoderInputGate(std::uint32_t capacity);
  ~EncoderInputGate() = default;
  EncoderInputGate(const EncoderInputGate&) = delete;
  EncoderInputGate& operator=(const EncoderInputGate&) = delete;

  std::optional<Lease> try_acquire() noexcept;
  Ticket* arm(Lease&& lease) noexcept;
  EncoderInputRecord* record(Ticket* ticket) noexcept;
  void settle(Ticket* ticket) noexcept;
  void close() noexcept;
  bool wait_until_empty(std::chrono::milliseconds timeout) noexcept;
  std::uint32_t capacity() const noexcept;
  std::uint32_t in_use() const noexcept;
  std::uint32_t high_water() const noexcept;
  std::uint64_t misses() const noexcept;

 private:
  friend class Lease;
  void record_release() noexcept;

  Pool pool_;
  std::unique_ptr<Ticket[]> tickets_;
  std::atomic_bool accepting_{true};
  std::atomic_uint32_t in_use_{};
  std::atomic_uint32_t high_water_{};
  std::atomic_uint64_t misses_{};
  std::mutex mutex_;
  std::condition_variable condition_;
};

struct MetalEncoderStats final {
  std::uint64_t submissions{};
  std::uint64_t completions{};
  std::uint64_t bytes{};
  std::uint64_t drops{};
  std::uint64_t rejected_frames{};
  std::uint64_t callback_errors{};
  std::uint32_t input_capacity{};
  std::uint32_t input_high_water{};
  std::uint32_t input_in_use{};
  bool using_hardware{};
  bool drain_timed_out{};
};

class MetalEncoder final {
 public:
  MetalEncoder(std::uint32_t width, std::uint32_t height,
               const swim::core::AppConfig& config,
               swim::core::RuntimeCounters& metrics);
  ~MetalEncoder();
  MetalEncoder(const MetalEncoder&) = delete;
  MetalEncoder& operator=(const MetalEncoder&) = delete;

  bool offer(MetalOutputLease output, CMTime pts) noexcept;
  void close_and_drain();
  MetalEncoderStats stats() const noexcept;
  bool has_fatal_error() const noexcept;
  std::string fatal_error_message() const;

 private:
  class Impl;
  std::shared_ptr<Impl> impl_;
};

}  // namespace swim::metal
