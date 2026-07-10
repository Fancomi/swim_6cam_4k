#include <swim/core/runtime_validation.hpp>

#include <array>
#include <cstddef>
#include <stdexcept>
#include <string_view>

namespace swim::core {
namespace {

constexpr std::uint32_t kLogicalWidth = 5001;
constexpr std::uint32_t kLogicalHeight = 2101;
constexpr std::uint32_t kEncodedWidth = 5002;
constexpr std::uint32_t kEncodedHeight = 2102;
constexpr std::array<std::string_view, 6> kCameraIds{
    "cam3", "cam2", "cam1", "cam4", "cam5", "cam6"};

}  // namespace

void validate_runtime_compatibility(const AppConfig& config,
                                    const RuntimeAsset& asset) {
  if (asset.logical_width != kLogicalWidth ||
      asset.logical_height != kLogicalHeight ||
      asset.encoded_width != kEncodedWidth ||
      asset.encoded_height != kEncodedHeight) {
    throw std::runtime_error(
        "runtime asset dimensions must be 5001x2101 -> 5002x2102");
  }
  if (asset.cameras.size() != kCameraIds.size()) {
    throw std::runtime_error("asset must contain exactly six cameras");
  }
  for (std::size_t index = 0; index < kCameraIds.size(); ++index) {
    if (config.sources[index].camera_id != kCameraIds[index] ||
        asset.cameras[index].camera_id != kCameraIds[index]) {
      throw std::runtime_error(
          "camera order must be cam3,cam2,cam1,cam4,cam5,cam6");
    }
  }
}

}  // namespace swim::core
