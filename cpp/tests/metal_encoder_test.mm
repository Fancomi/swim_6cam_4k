#include "test_support.hpp"

#include <swim/metal/metal_encoder.hpp>
#include <swim/core/config.hpp>
#include <swim/core/metrics.hpp>

#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#import <Metal/Metal.h>

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <span>
#include <thread>
#include <vector>

namespace {

bool append_bytes(void* context, std::span<const std::uint8_t> bytes) noexcept {
  auto& output = *static_cast<std::vector<std::uint8_t>*>(context);
  output.insert(output.end(), bytes.begin(), bytes.end());
  return true;
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
