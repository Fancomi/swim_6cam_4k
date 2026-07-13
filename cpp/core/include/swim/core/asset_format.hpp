#pragma once

#include <array>
#include <cstdint>

namespace swim::core::disk {

#pragma pack(push, 1)
struct AssetHeaderV1 {
  std::array<char, 8> magic;
  std::uint32_t version;
  std::uint32_t header_bytes;
  std::uint32_t logical_width;
  std::uint32_t logical_height;
  std::uint32_t encoded_width;
  std::uint32_t encoded_height;
  std::uint32_t camera_count;
  std::uint32_t camera_record_bytes;
  std::uint64_t camera_table_offset;
  std::uint64_t body_bytes;
  std::array<std::uint8_t, 32> source_sha256;
  std::uint32_t body_crc32;
  std::array<std::uint8_t, 28> reserved;
};

struct CameraRecordV1 {
  std::array<char, 16> camera_id;
  std::array<char, 32> node_name;
  std::uint32_t vertex_count;
  std::uint32_t index_count;
  std::uint32_t weight_x;
  std::uint32_t weight_y;
  std::uint32_t weight_width;
  std::uint32_t weight_height;
  std::uint64_t vertices_offset;
  std::uint64_t indices_offset;
  std::uint64_t weights_offset;
  std::uint64_t weights_bytes;
  std::array<std::uint8_t, 16> reserved;
};

struct VertexV1 {
  float output_x;
  float output_y;
  float u;
  float v;
};
#pragma pack(pop)

static_assert(sizeof(AssetHeaderV1) == 120);
static_assert(sizeof(CameraRecordV1) == 120);
static_assert(sizeof(VertexV1) == 16);

}  // namespace swim::core::disk
