#pragma once

#include <chrono>
#include <cstdint>

namespace swim::core {

enum class PixelFormat : std::uint8_t {
  nv12_video_range,
  nv12_full_range,
  bgra8,
};

enum class ColorMatrix : std::uint8_t {
  bt709,
  bt601,
  bt2020,
};

struct FrameMetadata {
  std::uint32_t camera_index{};
  std::uint32_t width{};
  std::uint32_t height{};
  std::uint64_t sequence{};
  std::uint64_t decoder_generation{};
  std::int64_t pts_value{};
  std::int64_t pts_timescale{};
  std::chrono::steady_clock::time_point arrived_at{};
  std::chrono::steady_clock::time_point decoded_at{};
  PixelFormat pixel_format{PixelFormat::nv12_video_range};
  ColorMatrix color_matrix{ColorMatrix::bt709};
  bool discontinuity{};
};

struct NativeLeaseOps {
  void (*retain)(void*) noexcept{};
  void (*release)(void*) noexcept{};
  std::uint32_t backend_tag{};
};

// Owns one adopted native reference. Construction and all move operations are
// non-allocating; copies retain the native surface for independent in-flight
// use.
class FrameLease {
 public:
  FrameLease() = default;
  FrameLease(void* native, NativeLeaseOps ops,
             FrameMetadata metadata) noexcept;
  FrameLease(const FrameLease& other);
  FrameLease& operator=(const FrameLease& other);
  FrameLease(FrameLease&& other) noexcept;
  FrameLease& operator=(FrameLease&& other) noexcept;
  ~FrameLease();

  explicit operator bool() const noexcept;
  const FrameMetadata& metadata() const noexcept;
  void* native(std::uint32_t expected_backend_tag) const;

 private:
  void reset() noexcept;

  void* native_{};
  NativeLeaseOps ops_{};
  FrameMetadata metadata_{};
};

}  // namespace swim::core
