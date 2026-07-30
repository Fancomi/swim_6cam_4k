"""Binary layout and reader for versioned swim runtime assets."""

import dataclasses
import struct
import zlib
from pathlib import Path
from typing import List, Tuple


MAGIC = b"SW4KAST\0"
VERSION = 1
HEADER = struct.Struct("<8s8I2Q32sI28s")
CAMERA = struct.Struct("<16s32s6I4Q16s")
VERTEX = struct.Struct("<4f")
INDEX = struct.Struct("<I")
WEIGHT = struct.Struct("<H")


@dataclasses.dataclass(frozen=True)
class Header:
    magic: bytes
    version: int
    header_bytes: int
    logical_width: int
    logical_height: int
    encoded_width: int
    encoded_height: int
    camera_count: int
    camera_record_bytes: int
    camera_table_offset: int
    body_bytes: int
    source_sha256: bytes
    body_crc32: int


@dataclasses.dataclass(frozen=True)
class CameraRecord:
    camera_id: str
    node_name: str
    vertex_count: int
    index_count: int
    weight_x: int
    weight_y: int
    weight_width: int
    weight_height: int
    vertices_offset: int
    indices_offset: int
    weights_offset: int
    weights_bytes: int


def _decode_fixed(value: bytes, label: str) -> str:
    try:
        return value.split(b"\0", 1)[0].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"asset {label} is not valid UTF-8") from error


def _check_blob(
    label: str, offset: int, size: int, minimum_offset: int, file_size: int
) -> None:
    if offset < minimum_offset or offset > file_size or size > file_size - offset:
        raise ValueError(f"asset {label} blob is out of bounds")


def read_header(path: Path) -> Tuple[Header, List[CameraRecord], bytes]:
    """Read and fully validate the v1 header, camera table, and body."""
    file_bytes = Path(path).read_bytes()
    if len(file_bytes) < HEADER.size:
        raise ValueError("asset header is truncated")
    unpacked = HEADER.unpack_from(file_bytes)
    header = Header(*unpacked[:-1])

    if header.magic != MAGIC:
        raise ValueError("asset magic mismatch")
    if header.version != VERSION:
        raise ValueError(f"unsupported asset version: {header.version}")
    if header.header_bytes != HEADER.size:
        raise ValueError("asset header size mismatch")
    if header.camera_record_bytes != CAMERA.size:
        raise ValueError("asset camera record size mismatch")
    if header.body_bytes != len(file_bytes) - HEADER.size:
        raise ValueError("asset body size mismatch")

    table_bytes = header.camera_count * CAMERA.size
    if (
        header.camera_table_offset < HEADER.size
        or header.camera_table_offset > len(file_bytes)
        or table_bytes > len(file_bytes) - header.camera_table_offset
    ):
        raise ValueError("asset camera table is out of bounds")
    calculated_crc32 = zlib.crc32(file_bytes[HEADER.size:])
    if calculated_crc32 != header.body_crc32:
        raise ValueError("asset body CRC32 mismatch")

    cameras = []
    for index in range(header.camera_count):
        offset = header.camera_table_offset + index * header.camera_record_bytes
        values = CAMERA.unpack_from(file_bytes, offset)
        camera = CameraRecord(
            _decode_fixed(values[0], "camera ID"),
            _decode_fixed(values[1], "node name"),
            *values[2:-1],
        )
        minimum_blob_offset = header.camera_table_offset + table_bytes
        _check_blob(
            "vertices",
            camera.vertices_offset,
            camera.vertex_count * VERTEX.size,
            minimum_blob_offset,
            len(file_bytes),
        )
        _check_blob(
            "indices",
            camera.indices_offset,
            camera.index_count * INDEX.size,
            minimum_blob_offset,
            len(file_bytes),
        )
        if camera.weights_bytes != camera.weight_width * camera.weight_height * WEIGHT.size:
            raise ValueError("asset weights blob size does not match its dimensions")
        _check_blob(
            "weights",
            camera.weights_offset,
            camera.weights_bytes,
            minimum_blob_offset,
            len(file_bytes),
        )
        cameras.append(camera)

    return header, cameras, file_bytes[HEADER.size:]
