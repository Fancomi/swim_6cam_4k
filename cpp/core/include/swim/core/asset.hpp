#pragma once

#include <swim/core/asset_format.hpp>

#include <array>
#include <cstdint>
#include <filesystem>
#include <string>
#include <vector>

namespace swim::core {

struct CameraAsset {
  std::string camera_id;
  std::string node_name;
  std::vector<disk::VertexV1> vertices;
  std::vector<std::uint32_t> indices;
  std::uint32_t weight_x;
  std::uint32_t weight_y;
  std::uint32_t weight_width;
  std::uint32_t weight_height;
  std::vector<std::uint16_t> weights;
};

struct RuntimeAsset {
  std::uint32_t logical_width;
  std::uint32_t logical_height;
  std::uint32_t encoded_width;
  std::uint32_t encoded_height;
  std::array<std::uint8_t, 32> source_sha256;
  std::vector<CameraAsset> cameras;
};

RuntimeAsset load_asset(const std::filesystem::path& path);

}  // namespace swim::core
