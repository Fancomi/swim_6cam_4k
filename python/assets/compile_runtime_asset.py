"""Compile baked mesh JSON into the GPU-oriented runtime asset format."""

import argparse
import hashlib
import json
import zlib
from pathlib import Path
from typing import Sequence

import numpy as np

from python.assets.asset_format import CAMERA, HEADER, INDEX, MAGIC, VERSION, VERTEX
from python.underwater.render import (
    bottom_dirty_rows,
    build_remap_clipped,
    seam_weights,
)
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


def _mesh_geometry(mesh, xmin, ymin, canvas_height, ppm):
    """Vertices in output pixels. `canvas_height` is the UNCROPPED height: y is
    measured from the top, so a bottom crop only shortens the canvas and never
    moves the content. Geometry that falls past the shortened canvas is clipped
    by the rasterizer, which is exactly the crop."""
    vertices = []
    indices = []
    vertex_indices = {}
    for triangle in mesh["triangles"]:
        for vertex in triangle:
            value = (
                (vertex["pos"][0] - xmin) * ppm,
                canvas_height - 1 - (vertex["pos"][1] - ymin) * ppm,
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


def resolve_bottom_crop(spec, coverage) -> int:
    """Rows to drop from the bottom: "auto" measures them, "none" is 0, or an
    explicit count. `coverage` is the union mask of every lane."""
    if spec is None or spec == "none":
        return 0
    if spec == "auto":
        return int(bottom_dirty_rows(coverage))
    rows = int(spec)
    if rows < 0:
        raise ValueError(f"crop_bottom must not be negative: {spec}")
    return rows


def compile_asset(
    mesh_json: Path,
    output: Path,
    camera_ids: Sequence[str],
    ppm: float,
    neg_v: bool = True,
    blend_px: float | None = None,
    clip_uv: bool = False,
    source_size: tuple[int, int] = (1280, 720),
    crop_bottom: str | int | None = None,
) -> dict:
    """Compile one baked mesh JSON file into runtime format v1.

    `neg_v` flips world Y, matching the pool bake. The underwater panorama is
    already upright, so it compiles with neg_v=False.

    `blend_px` selects the blend used to bake per-camera weights. None keeps the
    pool's distance-transform feather; a number uses the underwater renderer's
    hard vertical seam with that transition width.

    `clip_uv` excludes pixels whose source coordinate falls outside the real
    image from a lane's coverage, using `source_size` to decide. The GPU sampler
    is mirrored_repeat, so without this those pixels get mirrored texture; the
    offline renderer drops them (build_remap_clipped). Baking the exclusion into
    the weights suppresses them at zero runtime cost, because the shader
    multiplies by weight and the resolve pass normalizes by accumulated alpha.

    `crop_bottom` drops rows from the bottom of the canvas: "auto" measures the
    ragged uncovered rows the shorter planes leave, "none"/None keeps them, or
    pass an explicit count. The crop only shortens the canvas — geometry is
    measured from the top — so the runtime needs no crop pass of its own.

    Returns the compiled header geometry for callers that want to report it.
    """
    source_bytes = Path(mesh_json).read_bytes()
    meshes = json.loads(source_bytes)["meshes"]
    if len(camera_ids) != len(meshes):
        raise ValueError(
            f"camera count mismatch: {len(camera_ids)} IDs for {len(meshes)} meshes"
        )
    to_meters(meshes, unit_scale=1.0, neg_v=neg_v)
    xmin, xmax, ymin, ymax = world_bounds(meshes)
    canvas_width = int(round((xmax - xmin) * ppm)) + 1
    canvas_height = int(round((ymax - ymin) * ppm)) + 1

    source_width, source_height = source_size
    masks = []
    for mesh in meshes:
        if clip_uv:
            _, _, mask = build_remap_clipped(
                mesh, source_width, source_height, xmin, ymin, ppm,
                canvas_width, canvas_height
            )
        else:
            _, _, mask = build_remap(
                mesh, 1, 1, xmin, ymin, ppm, canvas_width, canvas_height
            )
        masks.append(mask)

    coverage = np.zeros((canvas_height, canvas_width), np.uint8)
    for mask in masks:
        coverage |= mask
    crop_rows = resolve_bottom_crop(crop_bottom, coverage)
    if crop_rows >= canvas_height:
        raise ValueError(
            f"crop_bottom {crop_rows} removes the whole {canvas_height}-row canvas"
        )
    logical_width = canvas_width
    logical_height = canvas_height - crop_rows
    encoded_width = logical_width + (logical_width & 1)
    encoded_height = logical_height + (logical_height & 1)

    if blend_px is None:
        weights = feather_weights(masks)
    else:
        weights = seam_weights(masks, blend_px)
    if crop_rows:
        # Weights are canvas-sized; drop the same rows so a lane's weight
        # texture still lines up with output pixel coordinates.
        weights = [weight[:logical_height] for weight in weights]

    compiled = []
    for camera_id, mesh, weight in zip(camera_ids, meshes, weights):
        vertices, indices, vertex_blob, index_blob = _mesh_geometry(
            mesh, xmin, ymin, canvas_height, ppm
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
    return {
        "logical_width": logical_width,
        "logical_height": logical_height,
        "encoded_width": encoded_width,
        "encoded_height": encoded_height,
        "canvas_height": canvas_height,
        "crop_rows": crop_rows,
        "camera_count": len(compiled),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_json", type=Path)
    parser.add_argument("output_swasset", type=Path)
    parser.add_argument("--camera-ids", nargs="+", default=DEFAULT_CAMERA_IDS)
    parser.add_argument("--ppm", type=float, default=100.0)
    parser.add_argument(
        "--no-neg-v",
        dest="neg_v",
        action="store_false",
        default=True,
        help="keep world Y as-is (the underwater panorama is already upright)",
    )
    parser.add_argument(
        "--blend-px",
        type=float,
        default=None,
        help="bake hard vertical seams with this transition width instead of the "
        "pool's distance feather; matches python.underwater.render --blend-px",
    )
    parser.add_argument(
        "--clip-uv",
        action="store_true",
        help="exclude pixels whose UV falls outside the source image, matching "
        "the offline renderer instead of letting the GPU mirror-sample them",
    )
    parser.add_argument(
        "--source-size",
        nargs=2,
        type=int,
        metavar=("W", "H"),
        default=(1280, 720),
        help="source frame size used by --clip-uv (default: %(default)s)",
    )
    parser.add_argument(
        "--crop-bottom",
        default="none",
        metavar="auto|none|N",
        help="drop bottom rows the shorter planes leave uncovered; 'auto' "
        "measures them (default: %(default)s)",
    )
    args = parser.parse_args()
    geometry = compile_asset(
        args.input_json,
        args.output_swasset,
        args.camera_ids,
        args.ppm,
        neg_v=args.neg_v,
        blend_px=args.blend_px,
        clip_uv=args.clip_uv,
        source_size=tuple(args.source_size),
        crop_bottom=args.crop_bottom,
    )
    print(
        f"{geometry['camera_count']} cameras, canvas "
        f"{geometry['logical_width']}x{geometry['canvas_height']} "
        f"- crop {geometry['crop_rows']} -> logical "
        f"{geometry['logical_width']}x{geometry['logical_height']} "
        f"-> encoded {geometry['encoded_width']}x{geometry['encoded_height']}"
    )


if __name__ == "__main__":
    main()
