#include "test_support.hpp"

#include <swim/metal/metal_encoder.hpp>
#include <swim/metal/metal_backend.hpp>
#include <swim/core/config.hpp>
#include <swim/core/hot_path_allocations.hpp>
#include <swim/core/metrics.hpp>

#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>

#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <span>
#include <mutex>
#include <thread>
#include <vector>

namespace {

bool append_bytes(void* context, std::span<const std::uint8_t> bytes) noexcept {
  auto& output = *static_cast<std::vector<std::uint8_t>*>(context);
  output.insert(output.end(), bytes.begin(), bytes.end());
  return true;
}

constexpr std::array<std::uint8_t, 3> kInjectedAccessUnit{2, 0x26, 0x01};

struct FakeWriter {
  bool append_succeeds{true};
  bool close_succeeds{true};
  std::uint64_t appended{};
  std::uint32_t closes{};
  std::mutex mutex;
  std::condition_variable condition;
  bool block_append{};
  bool append_entered{};
  bool release_append{};

  static bool append(void* context,
                     std::span<const std::uint8_t> bytes) noexcept {
    auto& self = *static_cast<FakeWriter*>(context);
    if (self.block_append) {
      std::unique_lock lock(self.mutex);
      self.append_entered = true;
      self.condition.notify_all();
      self.condition.wait(lock, [&self] { return self.release_append; });
    }
    if (!self.append_succeeds) {
      return false;
    }
    self.appended += bytes.size();
    return true;
  }

  static bool close(void* context) noexcept {
    auto& self = *static_cast<FakeWriter*>(context);
    ++self.closes;
    return self.close_succeeds;
  }
};

struct FakeClock {
  std::atomic_uint64_t next{100};
  static std::uint64_t now(void* context) noexcept {
    return static_cast<FakeClock*>(context)->next.fetch_add(100);
  }
};

struct FakeNativeSession {
  enum class EncodeBehavior {
    pending,
    reject,
    synchronous_flag_drop,
    synchronous_callback_drop,
    synchronous_output,
  };

  std::mutex mutex;
  std::condition_variable condition;
  EncodeBehavior behavior{EncodeBehavior::pending};
  bool complete_entered{};
  bool release_complete{};
  bool invalidated{};
  bool released{};
  bool output_during_complete{};
  swim::metal::MetalEncoderInjectedCallback callback{};
  void* callback_context{};
  void* source_frame{};

  static OSStatus encode(
      void* context, CVPixelBufferRef, CMTime, void* source_frame,
      VTEncodeInfoFlags* info_flags,
      swim::metal::MetalEncoderInjectedCallback callback,
      void* callback_context) noexcept {
    auto& self = *static_cast<FakeNativeSession*>(context);
    self.callback = callback;
    self.callback_context = callback_context;
    self.source_frame = source_frame;
    *info_flags = 0;
    if (self.behavior == EncodeBehavior::reject) {
      return -1234;
    }
    if (self.behavior == EncodeBehavior::synchronous_flag_drop) {
      *info_flags = kVTEncodeInfo_FrameDropped;
      return noErr;
    }
    if (self.behavior == EncodeBehavior::synchronous_callback_drop) {
      callback(callback_context, source_frame,
               {.kind = swim::metal::MetalEncoderInjectedOutputKind::frame_dropped});
      return noErr;
    }
    if (self.behavior == EncodeBehavior::synchronous_output) {
      callback(callback_context, source_frame,
               {.kind = swim::metal::MetalEncoderInjectedOutputKind::access_unit,
                .access_unit = kInjectedAccessUnit,
                .nal_length_bytes = 1});
    }
    return noErr;
  }

  static OSStatus complete(void* context) noexcept {
    auto& self = *static_cast<FakeNativeSession*>(context);
    {
      std::lock_guard lock(self.mutex);
      self.complete_entered = true;
      self.condition.notify_all();
    }
    if (self.output_during_complete) {
      self.callback(
          self.callback_context, self.source_frame,
          {.kind = swim::metal::MetalEncoderInjectedOutputKind::access_unit,
           .access_unit = kInjectedAccessUnit,
           .nal_length_bytes = 1});
    }
    std::unique_lock lock(self.mutex);
    self.condition.wait(lock, [&self] { return self.release_complete; });
    return noErr;
  }

  static void invalidate(void* context) noexcept {
    auto& self = *static_cast<FakeNativeSession*>(context);
    std::lock_guard lock(self.mutex);
    self.invalidated = true;
    self.condition.notify_all();
  }

  static void release(void* context) noexcept {
    auto& self = *static_cast<FakeNativeSession*>(context);
    std::lock_guard lock(self.mutex);
    self.released = true;
    self.condition.notify_all();
  }

  void callback_drop() {
    callback(callback_context, source_frame,
             {.kind = swim::metal::MetalEncoderInjectedOutputKind::frame_dropped});
  }

  void callback_output() {
    callback(callback_context, source_frame,
             {.kind = swim::metal::MetalEncoderInjectedOutputKind::access_unit,
              .access_unit = kInjectedAccessUnit,
              .nal_length_bytes = 1});
  }

  void unblock_complete() {
    std::lock_guard lock(mutex);
    release_complete = true;
    condition.notify_all();
  }

  void wait_until_released() {
    std::unique_lock lock(mutex);
    condition.wait(lock, [this] { return released; });
  }
};

struct LifetimeSentinel {
  std::mutex* mutex{};
  std::condition_variable* condition{};
  bool* destroyed{};
  ~LifetimeSentinel() {
    std::lock_guard lock(*mutex);
    *destroyed = true;
    condition->notify_all();
  }
};

swim::metal::MetalEncoderDependencies dependencies(
    FakeNativeSession& native, FakeWriter& writer, FakeClock& clock,
    std::chrono::milliseconds timeout = std::chrono::milliseconds{50},
    std::shared_ptr<void> lifetime_anchor = {}) {
  return {
      .native = {.context = &native,
                 .encode = &FakeNativeSession::encode,
                 .complete_frames = &FakeNativeSession::complete,
                 .invalidate = &FakeNativeSession::invalidate,
                 .release = &FakeNativeSession::release,
                 .using_hardware = true},
      .writer = {.context = &writer,
                 .append = &FakeWriter::append,
                 .close = &FakeWriter::close},
      .now_context = &clock,
      .now_ns = &FakeClock::now,
      .drain_timeout = timeout,
      .lifetime_anchor = std::move(lifetime_anchor),
  };
}

std::shared_ptr<swim::metal::MetalContext> test_metal_context() {
  auto context = std::make_shared<swim::metal::MetalContext>();
  context->device = MTLCreateSystemDefaultDevice();
  CHECK(context->device != nil);
  context->command_queue = [context->device newCommandQueue];
  CHECK(context->command_queue != nil);
  CHECK_EQ(CVMetalTextureCacheCreate(kCFAllocatorDefault, nullptr,
                                     context->device, nullptr,
                                     &context->texture_cache),
           kCVReturnSuccess);
  return context;
}

swim::core::AppConfig null_encode_config() {
  swim::core::AppConfig config;
  config.encode = true;
  config.encode_sink = swim::core::EncodeSink::null_sink;
  return config;
}

}  // namespace

TEST_CASE(encoder_input_saturation_drops_without_blocking_renderer) {
  swim::metal::EncoderInputGate gate{2};
  auto first = gate.try_acquire();
  auto second = gate.try_acquire();
  CHECK(first.has_value());
  CHECK(second.has_value());
  const auto started = std::chrono::steady_clock::now();
  CHECK(!gate.try_acquire().has_value());
  CHECK(std::chrono::steady_clock::now() - started <
        std::chrono::milliseconds{1});
  second.reset();
  CHECK(gate.try_acquire().has_value());
}

TEST_CASE(length_prefixed_writer_supports_four_byte_nal_lengths) {
  const std::array<std::uint8_t, 13> access_unit{
      0, 0, 0, 3, 0x65, 0x11, 0x22,
      0, 0, 0, 2, 0x41, 0x33};
  std::vector<std::uint8_t> output;
  const swim::metal::AnnexBWriter writer{&output, &append_bytes};
  CHECK(swim::metal::write_length_prefixed_nals_as_annex_b(
      access_unit, 4, writer));
  const std::vector<std::uint8_t> expected{
      0, 0, 0, 1, 0x65, 0x11, 0x22,
      0, 0, 0, 1, 0x41, 0x33};
  CHECK_EQ(output, expected);
}

TEST_CASE(length_prefixed_writer_supports_one_and_two_byte_nal_lengths) {
  const std::array<std::uint8_t, 7> one_byte{2, 0x26, 0x01,
                                             3, 0x02, 0x03, 0x04};
  const std::array<std::uint8_t, 9> two_byte{0, 2, 0x26, 0x01,
                                             0, 3, 0x02, 0x03, 0x04};
  const std::vector<std::uint8_t> expected{
      0, 0, 0, 1, 0x26, 0x01, 0, 0, 0, 1, 0x02, 0x03, 0x04};
  for (const auto [bytes, width] :
       std::array<std::pair<std::span<const std::uint8_t>, std::uint8_t>, 2>{
           std::pair{std::span<const std::uint8_t>{one_byte}, std::uint8_t{1}},
           std::pair{std::span<const std::uint8_t>{two_byte}, std::uint8_t{2}}}) {
    std::vector<std::uint8_t> output;
    const swim::metal::AnnexBWriter writer{&output, &append_bytes};
    CHECK(swim::metal::write_length_prefixed_nals_as_annex_b(bytes, width,
                                                              writer));
    CHECK_EQ(output, expected);
  }
}

TEST_CASE(length_prefixed_writer_rejects_invalid_width_truncation_and_zero) {
  std::vector<std::uint8_t> output;
  const swim::metal::AnnexBWriter writer{&output, &append_bytes};
  const std::array<std::uint8_t, 2> valid{1, 0x26};
  CHECK(!swim::metal::write_length_prefixed_nals_as_annex_b(valid, 0,
                                                             writer));
  CHECK(!swim::metal::write_length_prefixed_nals_as_annex_b(valid, 3,
                                                             writer));
  const std::array<std::uint8_t, 6> truncated{0, 0, 0, 4, 0x65, 0x11};
  CHECK(!swim::metal::write_length_prefixed_nals_as_annex_b(truncated, 4,
                                                             writer));
  const std::array<std::uint8_t, 4> zero_length{};
  CHECK(!swim::metal::write_length_prefixed_nals_as_annex_b(zero_length, 4,
                                                             writer));
}

TEST_CASE(length_prefixed_writer_rejects_an_empty_access_unit) {
  std::vector<std::uint8_t> output;
  const swim::metal::AnnexBWriter writer{&output, &append_bytes};
  CHECK(!swim::metal::write_length_prefixed_nals_as_annex_b({}, 4, writer));

  CMBlockBufferRef empty = nullptr;
  CHECK_EQ(CMBlockBufferCreateEmpty(kCFAllocatorDefault, 0, 0, &empty),
           kCMBlockBufferNoErr);
  CHECK(!swim::metal::write_length_prefixed_nals_as_annex_b(
      empty, 0, 4, writer));
  CFRelease(empty);
}

TEST_CASE(length_prefixed_writer_reads_a_nal_across_block_boundaries) {
  const std::array<std::uint8_t, 5> first{0, 0, 0, 5, 0x26};
  const std::array<std::uint8_t, 4> second{0x11, 0x22, 0x33, 0x44};
  CMBlockBufferRef block = nullptr;
  CHECK_EQ(CMBlockBufferCreateEmpty(kCFAllocatorDefault, 2, 0, &block),
           kCMBlockBufferNoErr);
  CHECK_EQ(CMBlockBufferAppendMemoryBlock(
               block, const_cast<std::uint8_t*>(first.data()), first.size(),
               kCFAllocatorNull, nullptr, 0, first.size(), 0),
           kCMBlockBufferNoErr);
  CHECK_EQ(CMBlockBufferAppendMemoryBlock(
               block, const_cast<std::uint8_t*>(second.data()), second.size(),
               kCFAllocatorNull, nullptr, 0, second.size(), 0),
           kCMBlockBufferNoErr);

  std::vector<std::uint8_t> output;
  const swim::metal::AnnexBWriter writer{&output, &append_bytes};
  CHECK(swim::metal::write_length_prefixed_nals_as_annex_b(
      block, first.size() + second.size(), 4, writer));
  CHECK_EQ(output, (std::vector<std::uint8_t>{
                       0, 0, 0, 1, 0x26, 0x11, 0x22, 0x33, 0x44}));
  CFRelease(block);
}

TEST_CASE(keyframe_writer_orders_vps_sps_pps_before_coded_slices) {
  const std::array<std::uint8_t, 2> vps{0x40, 0x01};
  const std::array<std::uint8_t, 2> sps{0x42, 0x01};
  const std::array<std::uint8_t, 2> pps{0x44, 0x01};
  const std::array<std::uint8_t, 4> access_unit{0, 2, 0x26, 0x01};
  const std::array<std::span<const std::uint8_t>, 3> parameter_sets{
      vps, sps, pps};
  std::vector<std::uint8_t> output;
  const swim::metal::AnnexBWriter writer{&output, &append_bytes};
  CHECK(swim::metal::write_hevc_access_unit_as_annex_b(
      access_unit, 2, parameter_sets, writer));
  CHECK_EQ(output, (std::vector<std::uint8_t>{
                       0, 0, 0, 1, 0x40, 0x01, 0, 0, 0, 1, 0x42, 0x01,
                       0, 0, 0, 1, 0x44, 0x01, 0, 0, 0, 1, 0x26, 0x01}));
}

TEST_CASE(callback_ticket_holds_output_lease_until_settlement) {
  auto context = std::make_shared<swim::metal::MetalContext>();
  context->device = MTLCreateSystemDefaultDevice();
  CHECK(context->device != nil);
  context->command_queue = [context->device newCommandQueue];
  CHECK(context->command_queue != nil);
  CHECK_EQ(CVMetalTextureCacheCreate(kCFAllocatorDefault, nullptr,
                                     context->device, nullptr,
                                     &context->texture_cache),
           kCVReturnSuccess);
  swim::metal::MetalOutputPool output_pool{context, 1, 2, 2};
  auto output = output_pool.try_acquire();
  CHECK(output.has_value());

  swim::metal::EncoderInputGate gate{1};
  auto input = gate.try_acquire();
  CHECK(input.has_value());
  input->operator->()->output = std::move(*output);
  auto* ticket = gate.arm(std::move(*input));
  output.reset();
  input.reset();
  CHECK(!output_pool.try_acquire().has_value());
  gate.settle(ticket);
  CHECK(output_pool.try_acquire().has_value());
}

TEST_CASE(encoder_input_drain_is_bounded_and_reports_timeout) {
  swim::metal::EncoderInputGate gate{1};
  auto input = gate.try_acquire();
  CHECK(input.has_value());
  auto* ticket = gate.arm(std::move(*input));
  gate.close();
  CHECK(!gate.try_acquire().has_value());
  CHECK(!gate.wait_until_empty(std::chrono::milliseconds{1}));
  gate.settle(ticket);
  CHECK(gate.wait_until_empty(std::chrono::milliseconds{10}));
}

TEST_CASE(hardware_encoder_requires_exact_canvas_and_reports_hardware) {
  swim::core::AppConfig config;
  config.encode = true;
  config.encode_sink = swim::core::EncodeSink::null_sink;
  swim::core::RuntimeCounters metrics;
  CHECK_THROWS_WITH(swim::metal::MetalEncoder(5000, 2102, config, metrics),
                    "HEVC encoder requires exact 5002x2102 output");
  swim::metal::MetalEncoder encoder(5002, 2102, config, metrics);
  CHECK(encoder.stats().using_hardware);
  encoder.close_and_drain();
  CHECK(!encoder.stats().drain_timed_out);
}

TEST_CASE(synchronous_and_callback_frame_drops_settle_without_fatal_error) {
  for (const auto behavior :
       {FakeNativeSession::EncodeBehavior::synchronous_flag_drop,
        FakeNativeSession::EncodeBehavior::synchronous_callback_drop}) {
    FakeNativeSession native;
    native.behavior = behavior;
    native.release_complete = true;
    FakeWriter writer;
    FakeClock clock;
    swim::core::RuntimeCounters metrics;
    swim::metal::MetalEncoder encoder(
        5002, 2102, null_encode_config(), metrics,
        dependencies(native, writer, clock));
    auto context = test_metal_context();
    swim::metal::MetalOutputPool pool{context, 1, 2, 2};
    auto output = pool.try_acquire();
    CHECK(output.has_value());
    static_cast<void>(encoder.offer(std::move(*output), CMTimeMake(0, 30000)));
    CHECK_EQ(encoder.stats().drops, 1u);
    CHECK_EQ(encoder.stats().callback_errors, 0u);
    CHECK_EQ(encoder.stats().input_in_use, 0u);
    CHECK(!encoder.has_fatal_error());
    encoder.close_and_drain();
  }
}

TEST_CASE(asynchronous_frame_drop_is_recoverable_and_settles_ticket) {
  FakeNativeSession native;
  native.release_complete = true;
  FakeWriter writer;
  FakeClock clock;
  swim::core::RuntimeCounters metrics;
  swim::metal::MetalEncoder encoder(5002, 2102, null_encode_config(), metrics,
                                    dependencies(native, writer, clock));
  auto context = test_metal_context();
  swim::metal::MetalOutputPool pool{context, 1, 2, 2};
  auto output = pool.try_acquire();
  CHECK(output.has_value());
  CHECK(encoder.offer(std::move(*output), CMTimeMake(0, 30000)));
  CHECK_EQ(encoder.stats().input_in_use, 1u);
  native.callback_drop();
  CHECK_EQ(encoder.stats().drops, 1u);
  CHECK_EQ(encoder.stats().callback_errors, 0u);
  CHECK_EQ(encoder.stats().input_in_use, 0u);
  CHECK(!encoder.has_fatal_error());
  encoder.close_and_drain();
}

TEST_CASE(rejected_frame_counts_reason_and_total_drop_once) {
  FakeNativeSession native;
  native.behavior = FakeNativeSession::EncodeBehavior::reject;
  native.release_complete = true;
  FakeWriter writer;
  FakeClock clock;
  swim::core::RuntimeCounters metrics;
  swim::metal::MetalEncoder encoder(5002, 2102, null_encode_config(), metrics,
                                    dependencies(native, writer, clock));
  auto context = test_metal_context();
  swim::metal::MetalOutputPool pool{context, 1, 2, 2};
  auto output = pool.try_acquire();
  CHECK(output.has_value());
  CHECK(!encoder.offer(std::move(*output), CMTimeMake(0, 30000)));
  CHECK_EQ(encoder.stats().rejected_frames, 1u);
  CHECK_EQ(encoder.stats().drops, 1u);
  CHECK_EQ(encoder.stats().input_in_use, 0u);
  encoder.close_and_drain();
}

TEST_CASE(synchronous_output_publishes_submit_time_before_completion) {
  FakeNativeSession native;
  native.behavior = FakeNativeSession::EncodeBehavior::synchronous_output;
  native.release_complete = true;
  FakeWriter writer;
  FakeClock clock;
  swim::core::RuntimeCounters metrics;
  swim::metal::MetalEncoder encoder(5002, 2102, null_encode_config(), metrics,
                                    dependencies(native, writer, clock));
  auto context = test_metal_context();
  swim::metal::MetalOutputPool pool{context, 1, 2, 2};
  auto output = pool.try_acquire();
  CHECK(output.has_value());
  CHECK(encoder.offer(std::move(*output), CMTimeMake(0, 30000)));
  encoder.close_and_drain();
  const auto snapshot = metrics.snapshot_and_reset();
  CHECK_EQ(snapshot.encode_submissions, 1u);
  CHECK_EQ(snapshot.encode_completions, 1u);
  CHECK(snapshot.encode_last_completion_ns > snapshot.encode_first_submit_ns);
  CHECK(snapshot.encode_completion_fps() > 0.0);
}

TEST_CASE(writer_append_failure_is_fatal_drops_once_and_stops_admission) {
  FakeNativeSession native;
  native.behavior = FakeNativeSession::EncodeBehavior::synchronous_output;
  native.release_complete = true;
  FakeWriter writer;
  writer.append_succeeds = false;
  FakeClock clock;
  swim::core::RuntimeCounters metrics;
  swim::metal::MetalEncoder encoder(5002, 2102, null_encode_config(), metrics,
                                    dependencies(native, writer, clock));
  auto context = test_metal_context();
  swim::metal::MetalOutputPool pool{context, 2, 2, 2};
  auto first = pool.try_acquire();
  CHECK(first.has_value());
  CHECK(encoder.offer(std::move(*first), CMTimeMake(0, 30000)));
  CHECK(encoder.has_fatal_error());
  CHECK(!swim::metal::metal_encoder_admits_render(encoder));
  CHECK_EQ(encoder.stats().callback_errors, 1u);
  CHECK_EQ(encoder.stats().drops, 1u);
  auto second = pool.try_acquire();
  CHECK(second.has_value());
  CHECK(!encoder.offer(std::move(*second), CMTimeMake(1001, 30000)));
  CHECK_EQ(encoder.stats().drops, 2u);
  encoder.close_and_drain();
}

TEST_CASE(writer_close_failure_is_fatal_and_counted_as_a_drop) {
  FakeNativeSession native;
  native.release_complete = true;
  FakeWriter writer;
  writer.close_succeeds = false;
  FakeClock clock;
  swim::core::RuntimeCounters metrics;
  swim::metal::MetalEncoder encoder(5002, 2102, null_encode_config(), metrics,
                                    dependencies(native, writer, clock));
  encoder.close_and_drain();
  CHECK(encoder.has_fatal_error());
  CHECK_EQ(encoder.stats().callback_errors, 1u);
  CHECK_EQ(encoder.stats().drops, 1u);
  CHECK_EQ(writer.closes, 1u);
}

TEST_CASE(total_drain_deadline_invalidates_and_late_callback_has_safe_lifetime) {
  FakeNativeSession native;
  FakeWriter writer;
  FakeClock clock;
  std::mutex sentinel_mutex;
  std::condition_variable sentinel_condition;
  bool sentinel_destroyed = false;
  auto sentinel = std::make_shared<LifetimeSentinel>();
  sentinel->mutex = &sentinel_mutex;
  sentinel->condition = &sentinel_condition;
  sentinel->destroyed = &sentinel_destroyed;
  std::weak_ptr<LifetimeSentinel> weak_sentinel = sentinel;
  auto context = test_metal_context();
  swim::metal::MetalOutputPool pool{context, 1, 2, 2};
  {
    swim::core::RuntimeCounters metrics;
    swim::metal::MetalEncoder encoder(
        5002, 2102, null_encode_config(), metrics,
        dependencies(native, writer, clock, std::chrono::milliseconds{0},
                     sentinel));
    sentinel.reset();
    auto output = pool.try_acquire();
    CHECK(output.has_value());
    CHECK(encoder.offer(std::move(*output), CMTimeMake(0, 30000)));
    encoder.close_and_drain();
    CHECK(encoder.stats().drain_timed_out);
    CHECK_EQ(encoder.stats().drops, 1u);
    CHECK(native.invalidated);
  }

  bool retired_before_late_callback = false;
  {
    std::unique_lock lock(sentinel_mutex);
    retired_before_late_callback = sentinel_condition.wait_for(
        lock, std::chrono::milliseconds{50},
        [&] { return sentinel_destroyed; });
  }
  if (!retired_before_late_callback) {
    native.callback_drop();
    native.unblock_complete();
    std::unique_lock lock(sentinel_mutex);
    static_cast<void>(sentinel_condition.wait_for(
        lock, std::chrono::seconds{1}, [&] { return sentinel_destroyed; }));
  }
  CHECK(retired_before_late_callback);
  CHECK(weak_sentinel.expired());
  CHECK(pool.try_acquire().has_value());
  native.callback_drop();
  native.unblock_complete();
  native.wait_until_released();
}

TEST_CASE(blocked_callback_writer_is_inside_the_total_drain_deadline) {
  FakeNativeSession native;
  FakeWriter writer;
  writer.block_append = true;
  FakeClock clock;
  std::mutex sentinel_mutex;
  std::condition_variable sentinel_condition;
  bool sentinel_destroyed = false;
  auto sentinel = std::make_shared<LifetimeSentinel>();
  sentinel->mutex = &sentinel_mutex;
  sentinel->condition = &sentinel_condition;
  sentinel->destroyed = &sentinel_destroyed;
  std::weak_ptr<LifetimeSentinel> weak_sentinel = sentinel;
  auto context = test_metal_context();
  swim::metal::MetalOutputPool pool{context, 1, 2, 2};
  {
    swim::core::RuntimeCounters metrics;
    swim::metal::MetalEncoder encoder(
        5002, 2102, null_encode_config(), metrics,
        dependencies(native, writer, clock, std::chrono::milliseconds{0},
                     sentinel));
    sentinel.reset();
    auto output = pool.try_acquire();
    CHECK(output.has_value());
    CHECK(encoder.offer(std::move(*output), CMTimeMake(0, 30000)));
    std::thread callback([&native] { native.callback_output(); });
    {
      std::unique_lock lock(writer.mutex);
      writer.condition.wait(lock,
                            [&writer] { return writer.append_entered; });
    }
    encoder.close_and_drain();
    CHECK(encoder.stats().drain_timed_out);
    CHECK(native.invalidated);
    {
      std::lock_guard lock(writer.mutex);
      writer.release_append = true;
      writer.condition.notify_all();
    }
    callback.join();
    native.unblock_complete();
    native.wait_until_released();
  }
  {
    std::unique_lock lock(sentinel_mutex);
    CHECK(sentinel_condition.wait_for(
        lock, std::chrono::seconds{1}, [&] { return sentinel_destroyed; }));
  }
  CHECK(weak_sentinel.expired());
  CHECK(pool.try_acquire().has_value());
}

TEST_CASE(timeout_detaches_external_metrics_before_blocked_callback_returns) {
  FakeNativeSession native;
  FakeWriter writer;
  writer.block_append = true;
  FakeClock clock;
  swim::core::RuntimeCounters metrics;
  swim::metal::MetalEncoder encoder(
      5002, 2102, null_encode_config(), metrics,
      dependencies(native, writer, clock, std::chrono::milliseconds{0}));
  auto context = test_metal_context();
  swim::metal::MetalOutputPool pool{context, 1, 2, 2};
  auto output = pool.try_acquire();
  CHECK(output.has_value());
  CHECK(encoder.offer(std::move(*output), CMTimeMake(0, 30000)));

  std::thread callback([&native] { native.callback_output(); });
  {
    std::unique_lock lock(writer.mutex);
    writer.condition.wait(lock, [&writer] { return writer.append_entered; });
  }
  encoder.close_and_drain();
  const auto at_timeout = metrics.sample_totals();
  {
    std::lock_guard lock(writer.mutex);
    writer.release_append = true;
    writer.condition.notify_all();
  }
  callback.join();
  const auto after_late_callback = metrics.sample_totals();

  native.unblock_complete();
  native.wait_until_released();

  CHECK_EQ(after_late_callback.encode_bytes, at_timeout.encode_bytes);
  CHECK_EQ(after_late_callback.encode_completions,
           at_timeout.encode_completions);
  CHECK_EQ(after_late_callback.encode_callback_errors,
           at_timeout.encode_callback_errors);
}

TEST_CASE(timeout_without_a_callback_retires_output_and_heavy_state) {
  FakeNativeSession native;
  FakeWriter writer;
  FakeClock clock;
  std::mutex sentinel_mutex;
  std::condition_variable sentinel_condition;
  bool sentinel_destroyed = false;
  auto sentinel = std::make_shared<LifetimeSentinel>();
  sentinel->mutex = &sentinel_mutex;
  sentinel->condition = &sentinel_condition;
  sentinel->destroyed = &sentinel_destroyed;
  std::weak_ptr<LifetimeSentinel> weak_sentinel = sentinel;
  auto context = test_metal_context();
  swim::metal::MetalOutputPool pool{context, 1, 2, 2};
  {
    swim::core::RuntimeCounters metrics;
    swim::metal::MetalEncoder encoder(
        5002, 2102, null_encode_config(), metrics,
        dependencies(native, writer, clock, std::chrono::milliseconds{0},
                     sentinel));
    sentinel.reset();
    auto output = pool.try_acquire();
    CHECK(output.has_value());
    CHECK(encoder.offer(std::move(*output), CMTimeMake(0, 30000)));
    encoder.close_and_drain();
    CHECK(encoder.stats().drain_timed_out);
    CHECK(native.invalidated);
  }
  native.unblock_complete();
  native.wait_until_released();
  bool destroyed = false;
  {
    std::unique_lock lock(sentinel_mutex);
    destroyed = sentinel_condition.wait_for(
        lock, std::chrono::milliseconds{50},
        [&] { return sentinel_destroyed; });
  }
  if (!destroyed) {
    native.callback_drop();
    std::unique_lock lock(sentinel_mutex);
    static_cast<void>(sentinel_condition.wait_for(
        lock, std::chrono::seconds{1}, [&] { return sentinel_destroyed; }));
  }
  CHECK(destroyed);
  CHECK(weak_sentinel.expired());
  CHECK(pool.try_acquire().has_value());
}

TEST_CASE(close_and_drain_performs_no_application_heap_allocation) {
  FakeNativeSession native;
  native.release_complete = true;
  FakeWriter writer;
  FakeClock clock;
  swim::core::RuntimeCounters metrics;
  swim::metal::MetalEncoder encoder(5002, 2102, null_encode_config(), metrics,
                                    dependencies(native, writer, clock));
  const auto before = swim::core::hot_path_allocation_count();
  {
    swim::core::HotPathAllocationScope scope;
    encoder.close_and_drain();
  }
  CHECK_EQ(swim::core::hot_path_allocation_count(), before);
}

TEST_CASE(complete_frames_flushes_a_cached_tail_before_gate_drain) {
  FakeNativeSession native;
  native.output_during_complete = true;
  native.release_complete = true;
  FakeWriter writer;
  FakeClock clock;
  swim::core::RuntimeCounters metrics;
  swim::metal::MetalEncoder encoder(
      5002, 2102, null_encode_config(), metrics,
      dependencies(native, writer, clock, std::chrono::milliseconds{50}));
  auto context = test_metal_context();
  swim::metal::MetalOutputPool pool{context, 1, 2, 2};
  auto output = pool.try_acquire();
  CHECK(output.has_value());
  CHECK(encoder.offer(std::move(*output), CMTimeMake(0, 30000)));
  encoder.close_and_drain();
  CHECK(native.complete_entered);
  CHECK(!encoder.stats().drain_timed_out);
  CHECK_EQ(encoder.stats().submissions, 1u);
  CHECK_EQ(encoder.stats().completions, 1u);
  CHECK_EQ(encoder.stats().drops, 0u);
  CHECK_EQ(encoder.stats().input_in_use, 0u);
  CHECK(pool.try_acquire().has_value());
}

TEST_CASE(blocked_complete_frames_cannot_exceed_the_total_deadline) {
  FakeNativeSession native;
  FakeWriter writer;
  FakeClock clock;
  swim::core::RuntimeCounters metrics;
  swim::metal::MetalEncoder encoder(
      5002, 2102, null_encode_config(), metrics,
      dependencies(native, writer, clock, std::chrono::milliseconds{0}));
  std::mutex drain_mutex;
  std::condition_variable drain_condition;
  bool drain_returned = false;
  std::thread drain([&] {
    encoder.close_and_drain();
    std::lock_guard lock(drain_mutex);
    drain_returned = true;
    drain_condition.notify_all();
  });
  {
    std::unique_lock lock(native.mutex);
    native.condition.wait(lock, [&native] { return native.complete_entered; });
  }
  bool returned_before_native_unblock = false;
  {
    std::unique_lock lock(drain_mutex);
    returned_before_native_unblock = drain_condition.wait_for(
        lock, std::chrono::milliseconds{50}, [&] { return drain_returned; });
  }
  native.unblock_complete();
  drain.join();
  native.wait_until_released();
  CHECK(returned_before_native_unblock);
  CHECK(encoder.stats().drain_timed_out);
  CHECK(native.invalidated);
}
