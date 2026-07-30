"""Compile a mesh JSON into the GPU runtime asset (.swasset).

Bakes exactly what the offline renderer computes — the same canvas, the same
per-lane coverage, the same blend weights — so the realtime stitch and the mp4
cannot drift apart. The runtime then only samples textures and multiplies by a
weight; it never re-derives geometry.
"""
import argparse
import hashlib
import json
import zlib
from pathlib import Path

import numpy as np

from python.stitch import compose as C
from python.stitch import profiles as P
from python.stitch.asset_format import (CAMERA, HEADER, INDEX, MAGIC, VERSION,
                                        VERTEX)


def _fixed(value, size, label):
    encoded = value.encode("utf-8")
    if len(encoded) >= size:
        raise ValueError(f"{label} is longer than {size - 1} UTF-8 bytes: {value!r}")
    return (encoded + b"\0").ljust(size, b"\0")


def _geometry(mesh, canvas):
    """Vertices in output pixels, deduplicated, plus the index list.

    y is measured from the top of the UNCROPPED canvas, so a bottom crop only
    shortens the raster and never moves content. Geometry past the shortened
    canvas is clipped by the rasterizer, which is exactly the crop."""
    vertices, indices, seen = [], [], {}
    for triangle in mesh["triangles"]:
        for vertex in triangle:
            value = ((vertex["pos"][0] - canvas.xmin) * canvas.ppm,
                     canvas.height - 1 - (vertex["pos"][1] - canvas.ymin) * canvas.ppm,
                     vertex["uv"][0], vertex["uv"][1])
            index = seen.get(value)
            if index is None:
                index = len(vertices)
                seen[value] = index
                vertices.append(value)
            indices.append(index)
    return (vertices, indices,
            b"".join(VERTEX.pack(*vertex) for vertex in vertices),
            b"".join(INDEX.pack(index) for index in indices))


def _crop_weight(weight):
    """Tight bounding box of the non-zero weights, as u16.

    A lane covers a fraction of the canvas, so storing the full-canvas float
    weight per lane would multiply the asset size by the lane count."""
    rows, columns = np.nonzero(weight > 0.0)
    if not len(columns):
        return 0, 0, 0, 0, b""
    x0, y0 = int(columns.min()), int(rows.min())
    x1, y1 = int(columns.max()) + 1, int(rows.max()) + 1
    scaled = np.rint(np.clip(weight[y0:y1, x0:x1], 0.0, 1.0) * 65535.0).astype("<u2")
    return x0, y0, x1 - x0, y1 - y0, scaled.tobytes(order="C")


def resolve_bottom_crop(spec, coverage):
    """Rows to drop: "auto" measures them, "none"/None is 0, else an explicit count."""
    if spec is None or spec == "none":
        return 0
    if spec == "auto":
        return int(C.bottom_dirty_rows(coverage))
    rows = int(spec)
    if rows < 0:
        raise ValueError(f"crop_bottom must not be negative: {spec}")
    return rows


def compile_asset(mesh_json, output, camera_ids, ppm, neg_v=False,
                  blend_px=None, clip_uv=False, source_size=(1280, 720),
                  crop_bottom=None):
    """Compile one mesh JSON into runtime format v1; returns the header geometry.

    `blend_px` None bakes the pool's distance feather, a number bakes the plane
    lines' vertical seam with that transition width. `clip_uv` excludes pixels
    whose source coordinate falls outside `source_size` from a lane's coverage:
    the GPU sampler is mirrored_repeat, so without it those pixels get mirrored
    texture, while the offline renderer drops them. Baking the exclusion into the
    weights suppresses them at zero runtime cost, because the shader multiplies by
    weight and the resolve pass normalizes by accumulated alpha.
    """
    source_bytes = Path(mesh_json).read_bytes()
    meshes = json.loads(source_bytes)["meshes"]
    if len(camera_ids) != len(meshes):
        raise ValueError(f"camera count mismatch: {len(camera_ids)} IDs for "
                         f"{len(meshes)} meshes")
    C.to_metres(meshes, 1.0, neg_v)
    # margin 0: the runtime canvas is the published output size, and the shader
    # clamps rather than indexing, so it needs no padding of its own.
    canvas = C.Canvas(meshes, ppm, margin=0)

    masks = [C.build_remap(mesh, canvas, source_size, clip=clip_uv)[2]
             for mesh in meshes]
    coverage = np.zeros(canvas.shape, np.uint8)
    for mask in masks:
        coverage |= mask
    crop_rows = resolve_bottom_crop(crop_bottom, coverage)
    if crop_rows >= canvas.height:
        raise ValueError(f"crop_bottom {crop_rows} removes the whole "
                         f"{canvas.height}-row canvas")
    logical_width = canvas.width
    logical_height = canvas.height - crop_rows
    # H.264 yuv420p needs even dimensions; the extra row/column is padding the
    # logical size records so consumers know where the content ends.
    encoded_width = logical_width + (logical_width & 1)
    encoded_height = logical_height + (logical_height & 1)

    weights = C.blend_weights(masks, blend_px)
    if crop_rows:
        # Weights are canvas-sized; drop the same rows so a lane's weight texture
        # still lines up with output pixel coordinates.
        weights = [weight[:logical_height] for weight in weights]

    compiled = []
    for camera_id, mesh, weight in zip(camera_ids, meshes, weights):
        vertices, indices, vertex_blob, index_blob = _geometry(mesh, canvas)
        x, y, width, height, weight_blob = _crop_weight(weight)
        compiled.append({
            "camera_id": camera_id, "node_name": mesh["node"],
            "vertex_count": len(vertices), "index_count": len(indices),
            "weight_x": x, "weight_y": y,
            "weight_width": width, "weight_height": height,
            "vertex_blob": vertex_blob, "index_blob": index_blob,
            "weight_blob": weight_blob,
        })

    offset = HEADER.size + len(compiled) * CAMERA.size
    records, blobs = [], []
    for camera in compiled:
        vertices_offset = offset
        offset += len(camera["vertex_blob"])
        indices_offset = offset
        offset += len(camera["index_blob"])
        weights_offset = offset
        offset += len(camera["weight_blob"])
        records.append(CAMERA.pack(
            _fixed(camera["camera_id"], 16, "camera ID"),
            _fixed(camera["node_name"], 32, "node name"),
            camera["vertex_count"], camera["index_count"],
            camera["weight_x"], camera["weight_y"],
            camera["weight_width"], camera["weight_height"],
            vertices_offset, indices_offset, weights_offset,
            len(camera["weight_blob"]), b"\0" * 16))
        blobs.extend((camera["vertex_blob"], camera["index_blob"],
                      camera["weight_blob"]))

    body = b"".join(records + blobs)
    header = HEADER.pack(
        MAGIC, VERSION, HEADER.size, logical_width, logical_height,
        encoded_width, encoded_height, len(compiled), CAMERA.size, HEADER.size,
        len(body), hashlib.sha256(source_bytes).digest(), zlib.crc32(body),
        b"\0" * 28)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(header + body)
    return {"logical_width": logical_width, "logical_height": logical_height,
            "encoded_width": encoded_width, "encoded_height": encoded_height,
            "canvas_height": canvas.height, "crop_rows": crop_rows,
            "camera_count": len(compiled)}


def compile_profile(profile, mesh_json=None, output=None, ppm=None,
                    blend_px=None, crop_bottom=None, clip_uv=None):
    """Compile a line's asset with its own shaping, overridable per argument."""
    geometry = compile_asset(
        mesh_json or profile.mesh_json,
        output or profile.asset,
        profile.camera_ids,
        profile.ppm if ppm is None else ppm,
        neg_v=profile.neg_v,
        blend_px=profile.blend_px if blend_px is None else blend_px,
        clip_uv=profile.clip_uv if clip_uv is None else clip_uv,
        source_size=profile.source_size,
        crop_bottom=profile.crop_bottom if crop_bottom is None else crop_bottom,
    )
    print(f"{geometry['camera_count']} cameras, canvas "
          f"{geometry['logical_width']}x{geometry['canvas_height']} "
          f"- crop {geometry['crop_rows']} -> logical "
          f"{geometry['logical_width']}x{geometry['logical_height']} "
          f"-> encoded {geometry['encoded_width']}x{geometry['encoded_height']}")
    return geometry


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compile a line's mesh JSON into a runtime .swasset")
    parser.add_argument("line", choices=P.names())
    parser.add_argument("--mesh", type=Path, default=None,
                        help="mesh JSON (default: the line's)")
    parser.add_argument("--out", type=Path, default=None,
                        help="output .swasset (default: the line's)")
    parser.add_argument("--ppm", type=float, default=None)
    parser.add_argument("--blend-px", type=float, default=None)
    parser.add_argument("--crop-bottom", default=None, metavar="auto|none|N")
    args = parser.parse_args(argv)
    compile_profile(P.get(args.line), mesh_json=args.mesh, output=args.out,
                    ppm=args.ppm, blend_px=args.blend_px,
                    crop_bottom=args.crop_bottom)


if __name__ == "__main__":
    main()
