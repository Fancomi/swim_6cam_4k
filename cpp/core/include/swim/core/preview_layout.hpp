#pragma once

#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <utility>

namespace swim::core {

// Preview window geometry, shared by every backend's presenter. The three
// camera lines differ in composite aspect by nearly 4x — pool is 2.4:1, the
// 16-plane underwater strip 9.1:1 — so a window size baked in as a constant
// fits at most one of them and stretches the others.
namespace preview {

// Nominal window width. Very wide composites trade width for the minimum
// legible height instead of being squashed to a few dozen pixels.
inline constexpr std::uint32_t kTargetWidth = 1280;
inline constexpr std::uint32_t kMinimumHeight = 180;

}  // namespace preview

// The window size to open a `width` x `height` composite at: the nominal width
// scaled to the composite's own aspect ratio, so an untouched window shows the
// whole stitch with no letterbox.
inline std::pair<std::uint32_t, std::uint32_t> preview_target_size(
    std::uint32_t width, std::uint32_t height) {
  if (width == 0 || height == 0) {
    throw std::invalid_argument("preview target needs nonzero dimensions");
  }
  auto target_width = preview::kTargetWidth;
  auto target_height = static_cast<std::uint32_t>(std::lround(
      static_cast<double>(preview::kTargetWidth) * height / width));
  if (target_height < preview::kMinimumHeight) {
    target_height = preview::kMinimumHeight;
    target_width = static_cast<std::uint32_t>(std::lround(
        static_cast<double>(preview::kMinimumHeight) * width / height));
  }
  return {target_width, target_height};
}

// A drawing rectangle inside a render target, in pixels.
struct PreviewViewport final {
  std::int32_t x{};
  std::int32_t y{};
  std::uint32_t width{};
  std::uint32_t height{};
};

// The largest `source`-shaped rectangle centred inside a `target_width` x
// `target_height` render target. Presenting through this turns a window the
// user dragged off-ratio into black bars rather than a stretched panorama:
// aspect is preserved by shrinking the content, never by distorting it.
//
// Backends that pin the window's aspect ratio still need this — the pin quantises
// to whole pixels, and a maximised or snapped window ignores it outright.
inline PreviewViewport preview_viewport(std::uint32_t target_width,
                                        std::uint32_t target_height,
                                        std::uint32_t source_width,
                                        std::uint32_t source_height) {
  if (source_width == 0 || source_height == 0) {
    throw std::invalid_argument("preview viewport needs a nonzero source");
  }
  if (target_width == 0 || target_height == 0) {
    return {};
  }
  const auto source_aspect =
      static_cast<double>(source_width) / static_cast<double>(source_height);
  const auto target_aspect =
      static_cast<double>(target_width) / static_cast<double>(target_height);

  auto width = target_width;
  auto height = target_height;
  if (target_aspect > source_aspect) {
    // Too wide: full height, bars left and right.
    width = static_cast<std::uint32_t>(
        std::lround(static_cast<double>(target_height) * source_aspect));
  } else {
    // Too tall: full width, bars top and bottom.
    height = static_cast<std::uint32_t>(
        std::lround(static_cast<double>(target_width) / source_aspect));
  }
  // Rounding can overshoot by a pixel on extreme ratios; a viewport wider than
  // its target is rejected outright by D3D11.
  width = width == 0 ? 1 : (width > target_width ? target_width : width);
  height = height == 0 ? 1 : (height > target_height ? target_height : height);
  return {static_cast<std::int32_t>((target_width - width) / 2),
          static_cast<std::int32_t>((target_height - height) / 2), width,
          height};
}

}  // namespace swim::core
