import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from python.assets.build_keypoint_preview import main
from python.assets.keypoint_preview import (
    DatasetFormatError,
    Rect,
    discover_dataset,
    exact_box,
    generate_preview,
    natural_sort_key,
    render_person_crop,
    square_crop_box,
    visible_keypoints,
)


def write_single_frame_dataset(root: Path, persons: list[dict[str, object]]) -> Path:
    session = root / "session"
    session.mkdir(parents=True, exist_ok=True)
    self_image = np.full((200, 200, 3), 32, dtype=np.uint8)
    if not cv2.imwrite(str(session / "frame.png"), self_image):
        raise OSError("cannot write test image")
    payload = {
        "images": [{"file_name": "frame.png", "width": 200, "height": 200}],
        "annotations": [{"image_idx": 0, "persons": persons}],
    }
    (session / "session.json").write_text(json.dumps(payload), encoding="utf-8")
    return session / "session.json"


class GeometryTests(unittest.TestCase):
    def test_visible_keypoints_excludes_hidden_and_non_finite_values(self):
        points = [[10.0, 20.0, 2], [15.0, 25.0, 0], [math.nan, 30.0, 2], [40.0, 50.0, 1]]

        self.assertEqual(visible_keypoints(points), ((10.0, 20.0), (40.0, 50.0)))

    def test_exact_box_uses_only_visible_keypoints(self):
        self.assertEqual(
            exact_box(((10.0, 20.0), (40.0, 50.0), (25.0, 35.0))),
            Rect(left=10.0, top=20.0, right=40.0, bottom=50.0),
        )

    def test_square_crop_applies_padding_and_clamps_to_image(self):
        crop = square_crop_box(Rect(10.0, 10.0, 30.0, 20.0), image_width=100, image_height=70)

        self.assertEqual(crop, Rect(left=0.0, top=0.0, right=70.0, bottom=70.0))

    def test_natural_sort_key_orders_frame_numbers_numerically(self):
        names = ["frame_0010.png", "frame_0002.png", "frame_0001.png"]

        self.assertEqual(
            sorted(names, key=natural_sort_key),
            ["frame_0001.png", "frame_0002.png", "frame_0010.png"],
        )

    def test_natural_sort_key_breaks_equivalent_keys_by_original_name(self):
        names = ["session_1", "session_01"]

        self.assertEqual(sorted(names, key=natural_sort_key), ["session_01", "session_1"])

    def test_render_person_crop_covers_fractional_bounds_with_a_square_raster(self):
        image = np.zeros((20, 20, 3), dtype=np.uint8)
        crop = Rect(left=1.2, top=2.0, right=6.2, bottom=7.0)

        rendered = render_person_crop(image, (), Rect(3.0, 4.0, 3.0, 4.0), crop)

        self.assertEqual(rendered.shape[:2], (6, 6))


class DatasetParsingTests(unittest.TestCase):
    def test_discover_dataset_sorts_frames_and_assigns_global_indices(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session = root / "session_2"
            session.mkdir()
            payload = {
                "images": [
                    {"file_name": "frame_0010.png", "width": 200, "height": 100},
                    {"file_name": "frame_0002.png", "width": 200, "height": 100},
                ],
                "annotations": [
                    {"image_idx": 0, "persons": [{"id": "late", "keypoints": [[10, 10, 2]]}]},
                    {"image_idx": 1, "persons": [{"id": "first", "keypoints": [[20, 20, 2]]}, {"id": "second", "keypoints": [[30, 30, 2]]}]},
                ],
            }
            (session / "session_2.json").write_text(json.dumps(payload), encoding="utf-8")

            dataset = discover_dataset(root)

        self.assertEqual((dataset.image_count, dataset.person_count), (2, 3))
        self.assertEqual(
            [(record.frame_name, record.person_id, record.image_index, record.person_index) for record in dataset.records],
            [("frame_0002.png", "first", 1, 1), ("frame_0002.png", "second", 1, 2), ("frame_0010.png", "late", 2, 3)],
        )

    def test_discover_dataset_rejects_annotation_image_idx_outside_images(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            session = root / "session"
            session.mkdir()
            payload = {
                "images": [{"file_name": "frame.png"}],
                "annotations": [{"image_idx": 1, "persons": [{"id": 9, "keypoints": [[1, 2, 2]]}]}],
            }
            (session / "session.json").write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(DatasetFormatError, "image_idx 1.*outside images range"):
                discover_dataset(root)

    def test_discover_dataset_rejects_file_name_outside_session(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            session = root / "session"
            session.mkdir(parents=True)
            outside = Path(temporary_directory) / "outside.png"
            outside.write_bytes(b"outside")

            for file_name in (str(outside), "../outside.png"):
                with self.subTest(file_name=file_name):
                    payload = {"images": [{"file_name": file_name}], "annotations": []}
                    (session / "session.json").write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(DatasetFormatError, "file_name must remain within session"):
                        discover_dataset(root)

    def test_discover_dataset_rejects_file_name_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            session = root / "session"
            session.mkdir(parents=True)
            outside = Path(temporary_directory) / "outside.png"
            outside.write_bytes(b"outside")
            (session / "linked.png").symlink_to(outside)
            payload = {"images": [{"file_name": "linked.png"}], "annotations": []}
            (session / "session.json").write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(DatasetFormatError, "file_name must remain within session"):
                discover_dataset(root)

    def test_discover_dataset_wraps_invalid_keypoint_scalars(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            annotation_path = write_single_frame_dataset(root, [{"id": 1, "keypoints": [[None, 10, 2]]}])

            with self.assertRaisesRegex(DatasetFormatError, "invalid keypoint value.*session.json"):
                discover_dataset(root)

            self.assertTrue(annotation_path.is_file())

    def test_discover_dataset_breaks_natural_sort_ties_deterministically(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for session_name in ("session_1", "session_01"):
                session = root / session_name
                session.mkdir()
                payload = {
                    "images": [{"file_name": "frame.png"}],
                    "annotations": [{"image_idx": 0, "persons": [{"id": session_name, "keypoints": [[10, 10, 2]]}]}],
                }
                (session / f"{session_name}.json").write_text(json.dumps(payload), encoding="utf-8")

            dataset = discover_dataset(root)

        self.assertEqual([record.session_name for record in dataset.records], ["session_01", "session_1"])


class GeneratorTests(unittest.TestCase):
    def test_generate_preview_writes_crop_html_and_skip_report(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            session = root / "164150_merged"
            output = Path(temporary_directory) / "preview"
            session.mkdir(parents=True)
            self.assertTrue(cv2.imwrite(str(session / "frame_0001.png"), np.full((200, 300, 3), 32, dtype=np.uint8)))
            payload = {"images": [{"file_name": "frame_0001.png", "width": 300, "height": 200}], "annotations": [{"image_idx": 0, "persons": [{"id": 7, "keypoints": [[100, 80, 2], [140, 120, 2]]}, {"id": 8, "keypoints": [[0, 0, 0], [10, 10, 0]]}]}]}
            (session / "164150_merged.json").write_text(json.dumps(payload), encoding="utf-8")

            summary = generate_preview(root, output)
            page = (output / "index.html").read_text(encoding="utf-8")
            report = json.loads((output / "report.json").read_text(encoding="utf-8"))

            self.assertEqual((summary.source_image_count, summary.source_person_count), (1, 2))
            self.assertEqual((summary.generated_count, summary.skipped_count), (1, 1))
            self.assertTrue((output / "crops" / "0001.jpg").is_file())
            self.assertIn('loading="lazy"', page)
            self.assertIn("IntersectionObserver", page)
            self.assertIn("红框：精准关键点框", page)
            self.assertEqual(report["skipped"][0]["person_index"], 2)
            self.assertEqual(report["skipped"][0]["reason"], "no_visible_keypoints")

    def test_generate_preview_script_escapes_embedded_cards_json(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            session = root / "session"
            output = Path(temporary_directory) / "preview"
            session.mkdir(parents=True)
            self.assertTrue(cv2.imwrite(str(session / "frame.png"), np.full((200, 200, 3), 32, dtype=np.uint8)))
            person_id = '</script><script>alert("injected")</script>'
            payload = {"images": [{"file_name": "frame.png", "width": 200, "height": 200}], "annotations": [{"image_idx": 0, "persons": [{"id": person_id, "keypoints": [[100, 100, 2]]}]}]}
            (session / "session.json").write_text(json.dumps(payload), encoding="utf-8")

            generate_preview(root, output)
            page = (output / "index.html").read_text(encoding="utf-8")
            cards_payload = page.split("const cards = ", 1)[1].split(";\n", 1)[0]

            self.assertNotIn("</script>", cards_payload.lower())
            self.assertEqual(json.loads(cards_payload)[0]["alt"], f"session frame.png person {person_id}")

    def test_generate_preview_decodes_each_source_image_once(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            session = root / "session"
            output = Path(temporary_directory) / "preview"
            session.mkdir(parents=True)
            self.assertTrue(cv2.imwrite(str(session / "frame.png"), np.full((200, 200, 3), 32, dtype=np.uint8)))
            payload = {"images": [{"file_name": "frame.png", "width": 200, "height": 200}], "annotations": [{"image_idx": 0, "persons": [{"id": 1, "keypoints": [[80, 80, 2]]}, {"id": 2, "keypoints": [[120, 120, 2]]}]}]}
            (session / "session.json").write_text(json.dumps(payload), encoding="utf-8")

            with patch("python.assets.keypoint_preview.cv2.imread", wraps=cv2.imread) as imread:
                summary = generate_preview(root, output)

            self.assertEqual(summary.generated_count, 2)
            self.assertEqual(imread.call_count, 1)

    def test_generate_preview_validates_crop_parameters_with_only_hidden_people(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            session = root / "session"
            session.mkdir(parents=True)
            payload = {"images": [{"file_name": "frame.png", "width": 200, "height": 200}], "annotations": [{"image_idx": 0, "persons": [{"id": 1, "keypoints": [[0, 0, 0]]}]}]}
            (session / "session.json").write_text(json.dumps(payload), encoding="utf-8")

            for arguments in ({"padding_ratio": -0.1}, {"padding_ratio": math.nan}, {"padding_ratio": math.inf}, {"minimum_side": 0}):
                with self.subTest(arguments=arguments):
                    with self.assertRaisesRegex(ValueError, "padding_ratio must be finite and non-negative and minimum_side must be positive"):
                        generate_preview(root, Path(temporary_directory) / "preview", **arguments)

    def test_generate_preview_rejects_output_inside_dataset_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            write_single_frame_dataset(root, [{"id": 1, "keypoints": [[100, 100, 2]]}])

            for output in (root, root / "preview"):
                with self.subTest(output=output):
                    with self.assertRaisesRegex(ValueError, "output directory must be outside dataset root"):
                        generate_preview(root, output)
                    self.assertFalse((output / "crops").exists())

    def test_generate_preview_treats_cv2_decode_errors_as_unreadable(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            write_single_frame_dataset(root, [{"id": 1, "keypoints": [[100, 100, 2]]}])
            output = Path(temporary_directory) / "preview"

            with patch("python.assets.keypoint_preview.cv2.imread", side_effect=cv2.error("decode failed")):
                summary = generate_preview(root, output)

            report = json.loads((output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual((summary.generated_count, summary.skipped_count), (0, 1))
            self.assertEqual(report["skipped"][0]["reason"], "unreadable_source_image")

    def test_generate_preview_keeps_previous_output_when_generation_fails(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            write_single_frame_dataset(root, [{"id": 1, "keypoints": [[100, 100, 2]]}])
            output = Path(temporary_directory) / "preview"
            (output / "crops").mkdir(parents=True)
            (output / "index.html").write_text("old index", encoding="utf-8")
            (output / "report.json").write_text("old report", encoding="utf-8")
            (output / "crops" / "0001.jpg").write_bytes(b"old crop")

            write_attempts = 0

            def partially_write_then_fail(destination, image, parameters):
                nonlocal write_attempts
                write_attempts += 1
                Path(destination).write_bytes(b"new partial crop")
                return False

            with patch("python.assets.keypoint_preview.cv2.imwrite", side_effect=partially_write_then_fail):
                with self.assertRaisesRegex(OSError, "cannot write preview crop"):
                    generate_preview(root, output)

            self.assertEqual((output / "index.html").read_text(encoding="utf-8"), "old index")
            self.assertEqual((output / "report.json").read_text(encoding="utf-8"), "old report")
            self.assertEqual((output / "crops" / "0001.jpg").read_bytes(), b"old crop")

    def test_generate_preview_replaces_old_crop_set_on_success(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            write_single_frame_dataset(root, [{"id": 1, "keypoints": [[100, 100, 2]]}])
            output = Path(temporary_directory) / "preview"
            (output / "crops").mkdir(parents=True)
            (output / "crops" / "9999.jpg").write_bytes(b"stale")

            generate_preview(root, output)

            self.assertFalse((output / "crops" / "9999.jpg").exists())
            self.assertEqual(len(list((output / "crops").glob("*.jpg"))), 1)

    def test_generated_html_has_a_native_image_source_fallback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "dataset"
            write_single_frame_dataset(root, [{"id": 1, "keypoints": [[100, 100, 2]]}])
            output = Path(temporary_directory) / "preview"

            generate_preview(root, output)
            page = (output / "index.html").read_text(encoding="utf-8")

            self.assertIn('<img loading="lazy" decoding="async" src="crops/0001.jpg"', page)
            self.assertIn('data-src="crops/0001.jpg"', page)


class CommandLineTests(unittest.TestCase):
    def test_main_rejects_a_missing_dataset_root(self):
        with patch.object(sys, "argv", ["build_keypoint_preview.py", "--dataset-root", "/path/that/does/not/exist"]):
            with self.assertRaisesRegex(SystemExit, "dataset root does not exist"):
                main()
