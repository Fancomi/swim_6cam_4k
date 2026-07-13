"""Compile baked mesh JSON into the GPU-oriented runtime asset format."""

import argparse
import hashlib
import json
import zlib
from pathlib import Path
from typing import Sequence

import numpy as np

from python.assets.asset_format import CAMERA, HEADER, INDEX, MAGIC, VERSION, VERTEX
from python.validation.reference_renderer import (
    build_remap,
    feather_weights,
    to_meters,
    world_bounds,
)


DEFAULT_CAMERA_IDS = ("cam3", "cam2", "cam1", "cam4", "cam5", "cam6")


def _fixed(value: str, size: int, label: str) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) >= size:
        raise ValueError(f"{label} is longer than {size - 1} UTF-8 bytes: {value!r}")
    return (encoded + b"\0").ljust(size, b"\0")


def _mesh_geometry(mesh, xmin, ymin, logical_height, ppm):
    vertices = []
    indices = []
    vertex_indices = {}
    for triangle in mesh["triangles"]:
        for vertex in triangle:
            value = (
                (vertex["pos"][0] - xmin) * ppm,
                logical_height - 1 - (vertex["pos"][1] - ymin) * ppm,
                vertex["uv"][0],
                vertex["uv"][1],
            )
            index = vertex_indices.get(value)
            if index is None:
                index = len(vertices)
                vertex_indices[value] = index
                vertices.append(value)
            indices.append(index)
    vertex_blob = b"".join(VERTEX.pack(*vertex) for vertex in vertices)
    index_blob = b"".join(INDEX.pack(index) for index in indices)
    return vertices, indices, vertex_blob, index_blob


def _crop_weight(weight):
    nonzero_y, nonzero_x = np.nonzero(weight > 0.0)
    if not len(nonzero_x):
        return 0, 0, 0, 0, b""
    x0 = int(nonzero_x.min())
    y0 = int(nonzero_y.min())
    x1 = int(nonzero_x.max()) + 1
    y1 = int(nonzero_y.max()) + 1
    cropped = weight[y0:y1, x0:x1]
    weight_u16 = np.rint(np.clip(cropped, 0.0, 1.0) * 65535.0).astype("<u2")
    return x0, y0, x1 - x0, y1 - y0, weight_u16.tobytes(order="C")


def compile_asset(
    mesh_json: Path,
    output: Path,
    camera_ids: Sequence[str],
    ppm: float,
) -> None:
    """Compile one baked mesh JSON file into runtime format v1."""
    source_bytes = Path(mesh_json).read_bytes()
    meshes = json.loads(source_bytes)["meshes"]
    if len(camera_ids) != len(meshes):
        raise ValueError(
            f"camera count mismatch: {len(camera_ids)} IDs for {len(meshes)} meshes"
        )
    to_meters(meshes, unit_scale=1.0, neg_v=True)
    xmin, xmax, ymin, ymax = world_bounds(meshes)
    logical_width = int(round((xmax - xmin) * ppm)) + 1
    logical_height = int(round((ymax - ymin) * ppm)) + 1
    encoded_width = logical_width + (logical_width & 1)
    encoded_height = logical_height + (logical_height & 1)

    masks = []
    for mesh in meshes:
        _, _, mask = build_remap(
            mesh, 1, 1, xmin, ymin, ppm, logical_width, logical_height
        )
        masks.append(mask)
    weights = feather_weights(masks)

    compiled = []
    for camera_id, mesh, weight in zip(camera_ids, meshes, weights):
        vertices, indices, vertex_blob, index_blob = _mesh_geometry(
            mesh, xmin, ymin, logical_height, ppm
        )
        weight_x, weight_y, weight_width, weight_height, weight_blob = _crop_weight(weight)
        compiled.append(
            {
                "camera_id": camera_id,
                "node_name": mesh["node"],
                "vertex_count": len(vertices),
                "index_count": len(indices),
                "weight_x": weight_x,
                "weight_y": weight_y,
                "weight_width": weight_width,
                "weight_height": weight_height,
                "vertex_blob": vertex_blob,
                "index_blob": index_blob,
                "weight_blob": weight_blob,
            }
        )

    next_offset = HEADER.size + len(compiled) * CAMERA.size
    records = []
    blobs = []
    for camera in compiled:
        vertices_offset = next_offset
        next_offset += len(camera["vertex_blob"])
        indices_offset = next_offset
        next_offset += len(camera["index_blob"])
        weights_offset = next_offset
        next_offset += len(camera["weight_blob"])
        records.append(
            CAMERA.pack(
                _fixed(camera["camera_id"], 16, "camera ID"),
                _fixed(camera["node_name"], 32, "node name"),
                camera["vertex_count"],
                camera["index_count"],
                camera["weight_x"],
                camera["weight_y"],
                camera["weight_width"],
                camera["weight_height"],
                vertices_offset,
                indices_offset,
                weights_offset,
                len(camera["weight_blob"]),
                b"\0" * 16,
            )
        )
        blobs.extend(
            (camera["vertex_blob"], camera["index_blob"], camera["weight_blob"])
        )

    body = b"".join(records + blobs)
    header = HEADER.pack(
        MAGIC,
        VERSION,
        HEADER.size,
        logical_width,
        logical_height,
        encoded_width,
        encoded_height,
        len(compiled),
        CAMERA.size,
        HEADER.size,
        len(body),
        hashlib.sha256(source_bytes).digest(),
        zlib.crc32(body),
        b"\0" * 28,
    )
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(header + body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json", type=Path)
    parser.add_argument("output_swasset", type=Path)
    parser.add_argument("--camera-ids", nargs="+", default=DEFAULT_CAMERA_IDS)
    parser.add_argument("--ppm", type=float, default=100.0)
    args = parser.parse_args()
    compile_asset(args.input_json, args.output_swasset, args.camera_ids, args.ppm)


if __name__ == "__main__":
    main()
