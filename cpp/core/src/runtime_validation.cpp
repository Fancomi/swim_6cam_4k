#include <swim/core/runtime_validation.hpp>

#include <cstddef>
#include <stdexcept>
#include <string>

namespace swim::core {
namespace {

// VideoToolbox HEVC refuses odd dimensions, and the runtime's output pool sizes
// IOSurfaces from these numbers, so keep a sane upper bound rather than trusting
// an arbitrary asset header.
constexpr std::uint32_t kMaxOutputDimension = 16384;

}  // namespace

void validate_runtime_compatibility(const AppConfig& config,
                                    const RuntimeAsset& asset,
                                    bool resolved_encode) {
  // Output geometry comes from the compiled asset. The runtime only checks the
  // header is self-consistent: encoded is the logical size rounded up to even,
  // which is what the encoder and the preview both assume.
  if (asset.logical_width == 0 || asset.logical_height == 0) {
    throw std::runtime_error("runtime asset logical dimensions must be nonzero");
  }
  if (asset.logical_width > kMaxOutputDimension ||
      asset.logical_height > kMaxOutputDimension) {
    throw std::runtime_error("runtime asset dimensions exceed " +
                             std::to_string(kMaxOutputDimension));
  }
  const auto expected_encoded_width =
      asset.logical_width + (asset.logical_width & 1U);
  const auto expected_encoded_height =
      asset.logical_height + (asset.logical_height & 1U);
  if (asset.encoded_width != expected_encoded_width ||
      asset.encoded_height != expected_encoded_height) {
    throw std::runtime_error(
        "runtime asset encoded dimensions must be the logical size rounded up "
        "to even: expected " +
        std::to_string(expected_encoded_width) + "x" +
        std::to_string(expected_encoded_height) + ", found " +
        std::to_string(asset.encoded_width) + "x" +
        std::to_string(asset.encoded_height));
  }

  // Camera identity is data. The config declares the lane order and ids; the
  // asset must agree exactly, since lane index selects both the decoder and the
  // mesh that consumes its frames.
  if (asset.cameras.empty() || asset.cameras.size() > kMaxCameras) {
    throw std::runtime_error("asset camera count must be between 1 and " +
                             std::to_string(kMaxCameras));
  }
  if (asset.cameras.size() != config.source_count) {
    throw std::runtime_error(
        "config declares " + std::to_string(config.source_count) +
        " sources but the asset contains " +
        std::to_string(asset.cameras.size()) + " cameras");
  }
  for (std::size_t index = 0; index < asset.cameras.size(); ++index) {
    if (config.sources[index].camera_id != asset.cameras[index].camera_id) {
      throw std::runtime_error(
          "camera order mismatch at lane " + std::to_string(index) +
          ": config has '" + config.sources[index].camera_id +
          "' but the asset has '" + asset.cameras[index].camera_id + "'");
    }
  }

  if (!resolved_encode) {
    return;
  }
  if (config.fps_num != 30000 || config.fps_den != 1001) {
    throw std::runtime_error(
        "HEVC encoding requires fps_num/fps_den=30000/1001");
  }
  if (config.encode_sink == EncodeSink::null_sink) {
    return;
  }
  if (config.encode_path.empty()) {
    throw std::runtime_error("file HEVC encoding requires --encode-path");
  }
  const auto extension = config.encode_path.extension();
  if (extension != ".h265" && extension != ".hevc") {
    throw std::runtime_error(
        "HEVC encode path extension must be .h265 or .hevc");
  }
}

}  // namespace swim::core
