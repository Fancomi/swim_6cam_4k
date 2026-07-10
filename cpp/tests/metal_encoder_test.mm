#include "test_support.hpp"

#include <swim/metal/metal_encoder.hpp>

#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <span>
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

TEST_CASE(avcc_writer_emits_annex_b_start_codes_for_every_nal) {
  const std::array<std::uint8_t, 13> avcc{
      0, 0, 0, 3, 0x65, 0x11, 0x22,
      0, 0, 0, 2, 0x41, 0x33};
  std::vector<std::uint8_t> output;
  const swim::metal::AnnexBWriter writer{&output, &append_bytes};
  CHECK(swim::metal::write_avcc_as_annex_b(avcc, 4, writer));
  const std::vector<std::uint8_t> expected{
      0, 0, 0, 1, 0x65, 0x11, 0x22,
      0, 0, 0, 1, 0x41, 0x33};
  CHECK_EQ(output, expected);
}

TEST_CASE(avcc_writer_rejects_truncated_and_zero_length_nals) {
  std::vector<std::uint8_t> output;
  const swim::metal::AnnexBWriter writer{&output, &append_bytes};
  const std::array<std::uint8_t, 6> truncated{0, 0, 0, 4, 0x65, 0x11};
  CHECK(!swim::metal::write_avcc_as_annex_b(truncated, 4, writer));
  const std::array<std::uint8_t, 4> zero_length{};
  CHECK(!swim::metal::write_avcc_as_annex_b(zero_length, 4, writer));
}
