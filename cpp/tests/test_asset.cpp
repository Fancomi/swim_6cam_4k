#include "test_support.hpp"

#include <swim/core/asset.hpp>

#include <cstddef>
#include <filesystem>
#include <fstream>
#include <span>
#include <stdexcept>
#include <vector>

namespace {

std::filesystem::path test_asset_path() {
  return SWIM_TEST_ASSET_PATH;
}

std::vector<std::byte> read_all_bytes(const std::filesystem::path& path) {
  std::ifstream input(path, std::ios::binary | std::ios::ate);
  if (!input) {
    throw std::runtime_error("cannot read test asset");
  }
  const auto size = static_cast<std::size_t>(input.tellg());
  std::vector<std::byte> bytes(size);
  input.seekg(0);
  input.read(reinterpret_cast<char*>(bytes.data()),
             static_cast<std::streamsize>(size));
  if (!input) {
    throw std::runtime_error("cannot read test asset");
  }
  return bytes;
}

void write_all_bytes(const std::filesystem::path& path,
                     std::span<const std::byte> bytes) {
  std::ofstream output(path, std::ios::binary | std::ios::trunc);
  output.write(reinterpret_cast<const char*>(bytes.data()),
               static_cast<std::streamsize>(bytes.size()));
  if (!output) {
    throw std::runtime_error("cannot write corrupt test asset");
  }
}

std::filesystem::path corrupt_asset_path() {
  const auto path = std::filesystem::temp_directory_path() / "corrupt.swasset";
  auto bytes = read_all_bytes(test_asset_path());
  bytes.back() ^= std::byte{0x01};
  write_all_bytes(path, bytes);
  return path;
}

}  // namespace

TEST_CASE(loads_real_asset_in_fixed_camera_order) {
  const auto asset = swim::core::load_asset(test_asset_path());
  CHECK_EQ(asset.logical_width, 5001u);
  CHECK_EQ(asset.logical_height, 2101u);
  CHECK_EQ(asset.encoded_width, 5002u);
  CHECK_EQ(asset.encoded_height, 2102u);
  CHECK_EQ(asset.cameras.size(), 6u);
  CHECK_EQ(asset.cameras[0].camera_id, "cam3");
  CHECK_EQ(asset.cameras[5].camera_id, "cam6");
}

TEST_CASE(rejects_corrupt_asset_crc) {
  CHECK_THROWS_WITH(swim::core::load_asset(corrupt_asset_path()),
                    "asset body CRC32 mismatch");
}
