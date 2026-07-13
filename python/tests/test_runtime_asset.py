import hashlib
from pathlib import Path
import tempfile
import unittest
import zlib

from python.assets.asset_format import (
    CAMERA,
    HEADER,
    INDEX,
    MAGIC,
    VERSION,
    VERTEX,
    WEIGHT,
    read_header,
)
from python.assets.compile_runtime_asset import compile_asset


FIXTURE = Path(__file__).parent / "fixtures" / "tiny_mesh.json"


class RuntimeAssetTest(unittest.TestCase):
    def compile_fixture(self, directory):
        output = Path(directory) / "tiny.swasset"
        compile_asset(FIXTURE, output, ("cam3", "cam2"), ppm=10.0)
        return output

    def rewrite_header(self, path, field, value):
        file_bytes = bytearray(path.read_bytes())
        values = list(HEADER.unpack_from(file_bytes))
        values[field] = value
        file_bytes[: HEADER.size] = HEADER.pack(*values)
        path.write_bytes(file_bytes)

    def rewrite_camera(self, path, camera_index, field, value):
        file_bytes = bytearray(path.read_bytes())
        header_values = list(HEADER.unpack_from(file_bytes))
        offset = header_values[9] + camera_index * CAMERA.size
        values = list(CAMERA.unpack_from(file_bytes, offset))
        values[field] = value
        file_bytes[offset : offset + CAMERA.size] = CAMERA.pack(*values)
        header_values[12] = zlib.crc32(file_bytes[HEADER.size :])
        file_bytes[: HEADER.size] = HEADER.pack(*header_values)
        path.write_bytes(file_bytes)

    def test_compiler_writes_valid_offsets_and_camera_order(self):
        with tempfile.TemporaryDirectory() as td:
            output = self.compile_fixture(td)

            header, cameras, body = read_header(output)
            file_bytes = output.read_bytes()

            self.assertEqual(header.magic, MAGIC)
            self.assertEqual(header.version, VERSION)
            self.assertEqual(header.header_bytes, HEADER.size)
            self.assertEqual(HEADER.size, 120)
            self.assertEqual(CAMERA.size, 120)
            self.assertEqual(VERTEX.size, 16)
            self.assertEqual(INDEX.size, 4)
            self.assertEqual(WEIGHT.size, 2)
            self.assertEqual(
                (
                    header.logical_width,
                    header.logical_height,
                    header.encoded_width,
                    header.encoded_height,
                ),
                (31, 31, 32, 32),
            )
            self.assertEqual(
                [camera.camera_id for camera in cameras], ["cam3", "cam2"]
            )
            self.assertEqual([camera.node_name for camera in cameras], ["03", "02"])
            self.assertEqual(header.camera_record_bytes, CAMERA.size)
            self.assertEqual(header.camera_table_offset, HEADER.size)
            self.assertEqual(header.body_bytes, len(body))
            self.assertEqual(
                header.source_sha256, hashlib.sha256(FIXTURE.read_bytes()).digest()
            )
            self.assertEqual(header.body_crc32, zlib.crc32(body))
            self.assertEqual(
                [
                    (
                        camera.weight_x,
                        camera.weight_y,
                        camera.weight_width,
                        camera.weight_height,
                    )
                    for camera in cameras
                ],
                [(0, 0, 11, 11), (20, 20, 11, 11)],
            )
            table_end = HEADER.size + len(cameras) * CAMERA.size
            for camera in cameras:
                self.assertEqual(camera.vertex_count, 3)
                self.assertEqual(camera.index_count, 3)
                self.assertEqual(
                    camera.weights_bytes,
                    camera.weight_width * camera.weight_height * WEIGHT.size,
                )
                for offset, size in (
                    (camera.vertices_offset, camera.vertex_count * VERTEX.size),
                    (camera.indices_offset, camera.index_count * INDEX.size),
                    (camera.weights_offset, camera.weights_bytes),
                ):
                    self.assertGreaterEqual(offset, table_end)
                    self.assertLessEqual(offset + size, len(file_bytes))
            self.assertEqual(
                VERTEX.unpack_from(file_bytes, cameras[0].vertices_offset),
                (0.0, 0.0, 0.0, 0.0),
            )
            self.assertEqual(
                [
                    INDEX.unpack_from(
                        file_bytes, cameras[0].indices_offset + index * INDEX.size
                    )[0]
                    for index in range(cameras[0].index_count)
                ],
                [0, 1, 2],
            )

    def test_compiler_rejects_camera_count_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "tiny.swasset"

            with self.assertRaisesRegex(ValueError, "camera count"):
                compile_asset(FIXTURE, output, ("cam3",), ppm=10.0)

    def test_compiler_rejects_camera_ids_longer_than_15_utf8_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "tiny.swasset"

            with self.assertRaisesRegex(ValueError, "camera ID"):
                compile_asset(FIXTURE, output, ("camera_identifier", "cam2"), ppm=10.0)

    def test_reader_rejects_wrong_magic(self):
        with tempfile.TemporaryDirectory() as td:
            output = self.compile_fixture(td)
            self.rewrite_header(output, 0, b"NOTASSET")

            with self.assertRaisesRegex(ValueError, "magic"):
                read_header(output)

    def test_reader_rejects_wrong_version(self):
        with tempfile.TemporaryDirectory() as td:
            output = self.compile_fixture(td)
            self.rewrite_header(output, 1, VERSION + 1)

            with self.assertRaisesRegex(ValueError, "version"):
                read_header(output)

    def test_reader_rejects_wrong_camera_record_size(self):
        with tempfile.TemporaryDirectory() as td:
            output = self.compile_fixture(td)
            self.rewrite_header(output, 8, CAMERA.size + 1)

            with self.assertRaisesRegex(ValueError, "camera record size"):
                read_header(output)

    def test_reader_rejects_corrupt_body(self):
        with tempfile.TemporaryDirectory() as td:
            output = self.compile_fixture(td)
            file_bytes = bytearray(output.read_bytes())
            file_bytes[-1] ^= 1
            output.write_bytes(file_bytes)

            with self.assertRaisesRegex(ValueError, "CRC32"):
                read_header(output)

    def test_reader_rejects_truncated_file(self):
        with tempfile.TemporaryDirectory() as td:
            output = self.compile_fixture(td)
            output.write_bytes(output.read_bytes()[:-1])

            with self.assertRaisesRegex(ValueError, "body size"):
                read_header(output)

    def test_reader_bounds_checks_every_blob(self):
        for field, label in ((8, "vertices"), (9, "indices"), (10, "weights")):
            with self.subTest(blob=label), tempfile.TemporaryDirectory() as td:
                output = self.compile_fixture(td)
                self.rewrite_camera(output, 0, field, len(output.read_bytes()) + 1)

                with self.assertRaisesRegex(ValueError, f"{label} blob"):
                    read_header(output)


if __name__ == "__main__":
    unittest.main()
