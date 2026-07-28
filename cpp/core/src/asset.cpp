#include <swim/core/asset.hpp>
#include <swim/core/camera_capacity.hpp>

#include <algorithm>
#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
#include <vector>

static_assert(std::endian::native == std::endian::little,
              "runtime asset format v1 requires a little-endian host");

namespace swim::core {
namespace {

constexpr std::array<char, 8> kMagic{'S', 'W', '4', 'K', 'A', 'S', 'T', '\0'};
constexpr std::uint32_t kVersion = 1;


constexpr auto make_crc32_table() {
  std::array<std::uint32_t, 256> table{};
  for (std::uint32_t value = 0; value < table.size(); ++value) {
    auto remainder = value;
    for (int bit = 0; bit < 8; ++bit) {
      remainder = (remainder >> 1U) ^
                  (0xEDB88320U & (0U - (remainder & 1U)));
    }
    table[value] = remainder;
  }
  return table;
}

constexpr auto kCrc32Table = make_crc32_table();

std::uint32_t crc32(const std::byte* data, std::size_t size) {
  std::uint32_t crc = 0xFFFFFFFFU;
  for (std::size_t index = 0; index < size; ++index) {
    const auto byte = std::to_integer<std::uint8_t>(data[index]);
    const auto table_index = static_cast<std::uint8_t>(crc ^ byte);
    crc = kCrc32Table[table_index] ^ (crc >> 8U);
  }
  return crc ^ 0xFFFFFFFFU;
}

std::vector<std::byte> read_file(const std::filesystem::path& path) {
  std::ifstream input(path, std::ios::binary | std::ios::ate);
  if (!input) {
    throw std::runtime_error("cannot open asset");
  }

  const auto end = input.tellg();
  if (end < std::streampos{0}) {
    throw std::runtime_error("cannot determine asset size");
  }
  const auto file_size = static_cast<std::uintmax_t>(end);
  if (file_size > std::numeric_limits<std::size_t>::max() ||
      file_size > static_cast<std::uintmax_t>(
                      std::numeric_limits<std::streamsize>::max())) {
    throw std::runtime_error("asset is too large");
  }

  std::vector<std::byte> bytes(static_cast<std::size_t>(file_size));
  input.seekg(0);
  input.read(reinterpret_cast<char*>(bytes.data()),
             static_cast<std::streamsize>(bytes.size()));
  if (!input) {
    throw std::runtime_error("cannot read asset");
  }
  return bytes;
}

std::uint64_t checked_multiply(std::uint64_t left, std::uint64_t right,
                               std::string_view label) {
  if (left != 0 && right > std::numeric_limits<std::uint64_t>::max() / left) {
    throw std::runtime_error("asset " + std::string(label) + " size overflow");
  }
  return left * right;
}

void check_blob(std::string_view label, std::uint64_t offset,
                std::uint64_t size, std::uint64_t minimum_offset,
                std::uint64_t file_size) {
  if (offset < minimum_offset || offset > file_size ||
      size > file_size - offset) {
    throw std::runtime_error("asset " + std::string(label) +
                             " blob is out of bounds");
  }
}

template <class DiskType>
DiskType copy_struct(const std::vector<std::byte>& bytes, std::size_t offset) {
  static_assert(std::is_trivially_copyable_v<DiskType>);
  DiskType value{};
  std::memcpy(&value, bytes.data() + offset, sizeof(value));
  return value;
}

template <std::size_t Size>
std::string copy_fixed_string(const std::array<char, Size>& value) {
  const auto end = std::find(value.begin(), value.end(), '\0');
  return {value.begin(), end};
}

template <class Element>
std::vector<Element> copy_elements(const std::vector<std::byte>& bytes,
                                   std::uint64_t offset,
                                   std::uint64_t count) {
  const auto element_count = static_cast<std::size_t>(count);
  std::vector<Element> values(element_count);
  if (!values.empty()) {
    std::memcpy(values.data(),
                bytes.data() + static_cast<std::size_t>(offset),
                values.size() * sizeof(Element));
  }
  return values;
}

}  // namespace

RuntimeAsset load_asset(const std::filesystem::path& path) {
  const auto bytes = read_file(path);
  if (bytes.size() < sizeof(disk::AssetHeaderV1)) {
    throw std::runtime_error("asset header is truncated");
  }

  const auto header = copy_struct<disk::AssetHeaderV1>(bytes, 0);
  if (header.magic != kMagic) {
    throw std::runtime_error("asset magic mismatch");
  }
  if (header.version != kVersion) {
    throw std::runtime_error("unsupported asset version: " +
                             std::to_string(header.version));
  }
  if (header.header_bytes != sizeof(disk::AssetHeaderV1)) {
    throw std::runtime_error("asset header size mismatch");
  }
  if (header.camera_record_bytes != sizeof(disk::CameraRecordV1)) {
    throw std::runtime_error("asset camera record size mismatch");
  }

  const auto file_size = static_cast<std::uint64_t>(bytes.size());
  const auto expected_body_bytes = file_size - sizeof(disk::AssetHeaderV1);
  if (header.body_bytes != expected_body_bytes) {
    throw std::runtime_error("asset body size mismatch");
  }
  if (header.camera_count == 0 || header.camera_count > kMaxCameras) {
    throw std::runtime_error("asset camera count must be between 1 and " +
                             std::to_string(kMaxCameras));
  }

  const auto camera_table_bytes = checked_multiply(
      header.camera_count, sizeof(disk::CameraRecordV1), "camera table");
  check_blob("camera table", header.camera_table_offset, camera_table_bytes,
             sizeof(disk::AssetHeaderV1), file_size);
  const auto minimum_blob_offset =
      header.camera_table_offset + camera_table_bytes;

  const auto body_offset = static_cast<std::size_t>(header.header_bytes);
  if (crc32(bytes.data() + body_offset, bytes.size() - body_offset) !=
      header.body_crc32) {
    throw std::runtime_error("asset body CRC32 mismatch");
  }

  RuntimeAsset asset{
      .logical_width = header.logical_width,
      .logical_height = header.logical_height,
      .encoded_width = header.encoded_width,
      .encoded_height = header.encoded_height,
      .source_sha256 = header.source_sha256,
      .cameras = {},
  };
  asset.cameras.reserve(header.camera_count);

  for (std::uint64_t camera_index = 0;
       camera_index < header.camera_count; ++camera_index) {
    const auto record_offset = header.camera_table_offset +
                               camera_index * header.camera_record_bytes;
    const auto record = copy_struct<disk::CameraRecordV1>(
        bytes, static_cast<std::size_t>(record_offset));

    const auto vertices_bytes = checked_multiply(
        record.vertex_count, sizeof(disk::VertexV1), "vertices");
    const auto indices_bytes = checked_multiply(
        record.index_count, sizeof(std::uint32_t), "indices");
    const auto weight_elements = checked_multiply(
        record.weight_width, record.weight_height, "weights");
    const auto expected_weights_bytes = checked_multiply(
        weight_elements, sizeof(std::uint16_t), "weights");
    if (record.weights_bytes != expected_weights_bytes) {
      throw std::runtime_error(
          "asset weights blob size does not match its dimensions");
    }

    check_blob("vertices", record.vertices_offset, vertices_bytes,
               minimum_blob_offset, file_size);
    check_blob("indices", record.indices_offset, indices_bytes,
               minimum_blob_offset, file_size);
    check_blob("weights", record.weights_offset, record.weights_bytes,
               minimum_blob_offset, file_size);

    asset.cameras.push_back(CameraAsset{
        .camera_id = copy_fixed_string(record.camera_id),
        .node_name = copy_fixed_string(record.node_name),
        .vertices = copy_elements<disk::VertexV1>(
            bytes, record.vertices_offset, record.vertex_count),
        .indices = copy_elements<std::uint32_t>(
            bytes, record.indices_offset, record.index_count),
        .weight_x = record.weight_x,
        .weight_y = record.weight_y,
        .weight_width = record.weight_width,
        .weight_height = record.weight_height,
        .weights = copy_elements<std::uint16_t>(bytes, record.weights_offset,
                                                weight_elements),
    });
  }

  return asset;
}

}  // namespace swim::core
