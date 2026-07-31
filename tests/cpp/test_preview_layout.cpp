#include "test_support.hpp"

#include <swim/core/preview_layout.hpp>

#include <cmath>
#include <cstdint>
#include <stdexcept>

namespace {

// The three camera lines' composite sizes, from `--validate-only`. Their aspect
// ratios span nearly 4x, which is why the window cannot be a constant.
constexpr std::uint32_t kPoolWidth = 5002;
constexpr std::uint32_t kPoolHeight = 2102;
constexpr std::uint32_t kOverheadWidth = 4252;
constexpr std::uint32_t kOverheadHeight = 512;
constexpr std::uint32_t kUnderwaterWidth = 6002;
constexpr std::uint32_t kUnderwaterHeight = 656;

double aspect(std::uint32_t width, std::uint32_t height) {
  return static_cast<double>(width) / static_cast<double>(height);
}

void check_matches_source_aspect(std::uint32_t source_width,
                                 std::uint32_t source_height) {
  const auto size =
      swim::core::preview_target_size(source_width, source_height);
  const auto wanted = aspect(source_width, source_height);
  const auto got = aspect(size.first, size.second);
  CHECK(std::abs(got - wanted) / wanted < 0.01);
}

void check_viewport_matches_source_aspect(std::uint32_t target_width,
                                          std::uint32_t target_height,
                                          std::uint32_t source_width,
                                          std::uint32_t source_height) {
  const auto box = swim::core::preview_viewport(target_width, target_height,
                                                source_width, source_height);
  CHECK(box.width > 0);
  CHECK(box.height > 0);
  // Never larger than what it draws into, or D3D11 rejects the viewport.
  CHECK(box.width <= target_width);
  CHECK(box.height <= target_height);
  CHECK(static_cast<std::uint32_t>(box.x) + box.width <= target_width);
  CHECK(static_cast<std::uint32_t>(box.y) + box.height <= target_height);
  const auto wanted = aspect(source_width, source_height);
  const auto got = aspect(box.width, box.height);
  // One pixel of rounding on a 180px-tall strip is over half a percent.
  CHECK(std::abs(got - wanted) / wanted < 0.02);
}

}  // namespace

TEST_CASE(preview_target_keeps_each_line_composite_aspect) {
  check_matches_source_aspect(kPoolWidth, kPoolHeight);
  check_matches_source_aspect(kOverheadWidth, kOverheadHeight);
  check_matches_source_aspect(kUnderwaterWidth, kUnderwaterHeight);
}

TEST_CASE(preview_target_uses_the_nominal_width_when_height_allows) {
  const auto size = swim::core::preview_target_size(kPoolWidth, kPoolHeight);
  CHECK_EQ(size.first, 1280u);
}

TEST_CASE(preview_target_trades_width_for_a_legible_minimum_height) {
  // 1280 wide at 9.1:1 would be 140px tall. Widening instead keeps the aspect
  // and clears the floor; a clamped height alone would have stretched it.
  const auto size =
      swim::core::preview_target_size(kUnderwaterWidth, kUnderwaterHeight);
  CHECK(size.second >= 180u);
  CHECK(size.first > 1280u);
  check_matches_source_aspect(kUnderwaterWidth, kUnderwaterHeight);
}

TEST_CASE(preview_target_rejects_a_degenerate_composite) {
  CHECK_THROWS_WITH(swim::core::preview_target_size(0, 100),
                    "preview target needs nonzero dimensions");
}

TEST_CASE(preview_viewport_fills_a_window_opened_at_the_target_size) {
  // The untouched window: content fills it, no bars.
  const auto size = swim::core::preview_target_size(kPoolWidth, kPoolHeight);
  const auto box = swim::core::preview_viewport(size.first, size.second,
                                                kPoolWidth, kPoolHeight);
  CHECK_EQ(box.x, 0);
  CHECK_EQ(box.y, 0);
  CHECK_EQ(box.width, size.first);
  CHECK_EQ(box.height, size.second);
}

TEST_CASE(preview_viewport_letterboxes_a_window_dragged_too_tall) {
  // Bars top and bottom, full width, content centred.
  const auto box =
      swim::core::preview_viewport(1280, 1280, kPoolWidth, kPoolHeight);
  CHECK_EQ(box.width, 1280u);
  CHECK(box.height < 1280u);
  CHECK_EQ(box.x, 0);
  CHECK(box.y > 0);
  CHECK_EQ(static_cast<std::uint32_t>(box.y) * 2u + box.height <= 1280u, true);
  check_viewport_matches_source_aspect(1280, 1280, kPoolWidth, kPoolHeight);
}

TEST_CASE(preview_viewport_pillarboxes_a_window_dragged_too_wide) {
  // Bars left and right, full height, content centred.
  const auto box =
      swim::core::preview_viewport(2560, 400, kPoolWidth, kPoolHeight);
  CHECK_EQ(box.height, 400u);
  CHECK(box.width < 2560u);
  CHECK_EQ(box.y, 0);
  CHECK(box.x > 0);
  check_viewport_matches_source_aspect(2560, 400, kPoolWidth, kPoolHeight);
}

TEST_CASE(preview_viewport_holds_aspect_for_a_maximised_window) {
  // Maximising ignores any aspect pin the window manager was given, so the
  // presenter is the only thing standing between the user and a stretched
  // panorama. 2560x1440 is the common laptop-plus-monitor case.
  check_viewport_matches_source_aspect(2560, 1440, kPoolWidth, kPoolHeight);
  check_viewport_matches_source_aspect(2560, 1440, kOverheadWidth,
                                       kOverheadHeight);
  check_viewport_matches_source_aspect(2560, 1440, kUnderwaterWidth,
                                       kUnderwaterHeight);
}

TEST_CASE(preview_viewport_survives_a_collapsed_or_degenerate_target) {
  // A minimised window reports a zero client area; drawing nothing beats
  // handing D3D11 a zero-sized viewport.
  const auto empty =
      swim::core::preview_viewport(0, 0, kPoolWidth, kPoolHeight);
  CHECK_EQ(empty.width, 0u);
  CHECK_EQ(empty.height, 0u);

  // A one-pixel sliver still yields a legal viewport, never a zero extent.
  const auto sliver =
      swim::core::preview_viewport(1, 400, kUnderwaterWidth, kUnderwaterHeight);
  CHECK(sliver.width >= 1u);
  CHECK(sliver.height >= 1u);
  CHECK(sliver.width <= 1u);
  CHECK(sliver.height <= 400u);

  CHECK_THROWS_WITH(swim::core::preview_viewport(1280, 720, 0, 0),
                    "preview viewport needs a nonzero source");
}
