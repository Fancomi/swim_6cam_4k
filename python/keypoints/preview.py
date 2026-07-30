"""Geometry and data-record contracts for keypoint preview generation.

Consumes ``kpt-label/v1`` keypoints shaped as ``[x, y, visibility]``.
"""

import json
import math
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import cv2
import numpy as np

from python.common.page import escape


class DatasetFormatError(ValueError):
    """Raised when a dataset session's JSON payload violates the kpt-label/v1 contract."""


@dataclass(frozen=True)
class Rect:
    left: float
    top: float
    right: float
    bottom: float

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top


@dataclass(frozen=True)
class PersonRecord:
    session_name: str
    frame_name: str
    source_path: Path
    image_index: int
    person_index: int
    person_id: int | str
    keypoints: tuple[tuple[float, float, int], ...]


@dataclass(frozen=True)
class DatasetIndex:
    image_count: int
    person_count: int
    records: tuple[PersonRecord, ...]


def natural_sort_key(value: str) -> tuple[tuple[int, int | str], ...]:
    normalized = tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )
    return normalized + ((2, value),)


def visible_keypoints(keypoints: Iterable[Sequence[float]]) -> tuple[tuple[float, float], ...]:
    visible: list[tuple[float, float]] = []
    for point in keypoints:
        if len(point) < 3:
            continue
        x, y, visibility = float(point[0]), float(point[1]), int(point[2])
        if visibility > 0 and math.isfinite(x) and math.isfinite(y):
            visible.append((x, y))
    return tuple(visible)


def exact_box(points: Sequence[tuple[float, float]]) -> Rect | None:
    if not points:
        return None
    xs, ys = zip(*points)
    return Rect(min(xs), min(ys), max(xs), max(ys))


def _validate_crop_parameters(padding_ratio: float, minimum_side: int) -> None:
    if not math.isfinite(padding_ratio) or padding_ratio < 0 or minimum_side <= 0:
        raise ValueError("padding_ratio must be finite and non-negative and minimum_side must be positive")


def square_crop_box(
    exact: Rect,
    image_width: int,
    image_height: int,
    padding_ratio: float = 0.60,
    minimum_side: int = 160,
) -> Rect:
    if image_width <= 0 or image_height <= 0:
        raise ValueError("image dimensions must be positive")
    _validate_crop_parameters(padding_ratio, minimum_side)
    requested_side = max(max(exact.width, exact.height) * (1.0 + 2.0 * padding_ratio), float(minimum_side))
    side = min(requested_side, float(image_width), float(image_height))
    center_x = (exact.left + exact.right) / 2.0
    center_y = (exact.top + exact.bottom) / 2.0
    left = min(max(center_x - side / 2.0, 0.0), image_width - side)
    top = min(max(center_y - side / 2.0, 0.0), image_height - side)
    return Rect(left, top, left + side, top + side)


def _coerce_keypoints(keypoints: object, context: str) -> tuple[tuple[float, float, int], ...]:
    if not isinstance(keypoints, list):
        raise DatasetFormatError(f"keypoints must be a list in {context}")
    coerced: list[tuple[float, float, int]] = []
    for point in keypoints:
        if not isinstance(point, (list, tuple)) or len(point) < 3:
            raise DatasetFormatError(f"each keypoint must have at least three elements in {context}")
        try:
            x = float(point[0])
            y = float(point[1])
            visibility = int(point[2])
        except (TypeError, ValueError, OverflowError):
            raise DatasetFormatError(f"invalid keypoint value in {context}") from None
        coerced.append((x, y, visibility))
    return tuple(coerced)


def _annotation_by_image_index(annotations: list[object]) -> dict[int, dict[str, object]]:
    annotation_map: dict[int, dict[str, object]] = {}
    for annotation in annotations:
        if not isinstance(annotation, dict):
            raise DatasetFormatError("each annotation must be an object")
        image_idx = annotation.get("image_idx")
        if not isinstance(image_idx, int) or isinstance(image_idx, bool):
            raise DatasetFormatError("annotation image_idx must be an integer")
        if image_idx in annotation_map:
            raise DatasetFormatError(f"duplicate annotation for image_idx {image_idx}")
        annotation_map[image_idx] = annotation
    return annotation_map


def discover_dataset(dataset_root: Path) -> DatasetIndex:
    if not dataset_root.is_dir():
        raise DatasetFormatError(f"dataset root does not exist: {dataset_root}")
    staged: list[tuple[str, str, Path, Path, list[object]]] = []
    for session_dir in sorted(
        (path for path in dataset_root.iterdir() if path.is_dir()),
        key=lambda path: natural_sort_key(path.name),
    ):
        annotation_paths = sorted(session_dir.glob("*.json"), key=lambda path: natural_sort_key(path.name))
        if len(annotation_paths) != 1:
            raise DatasetFormatError(f"expected one JSON file in {session_dir}")
        with annotation_paths[0].open(encoding="utf-8") as annotation_file:
            payload = json.load(annotation_file)
        images = payload.get("images") if isinstance(payload, dict) else None
        annotations = payload.get("annotations") if isinstance(payload, dict) else None
        if not isinstance(images, list) or not isinstance(annotations, list):
            raise DatasetFormatError(f"missing images or annotations in {annotation_paths[0]}")
        annotation_map = _annotation_by_image_index(annotations)
        for image_idx in annotation_map:
            if image_idx < 0 or image_idx >= len(images):
                raise DatasetFormatError(f"annotation image_idx {image_idx} is outside images range")
        resolved_session_dir = session_dir.resolve()
        for source_index, image in enumerate(images):
            if not isinstance(image, dict) or not isinstance(image.get("file_name"), str):
                raise DatasetFormatError(f"invalid image entry in {annotation_paths[0]}")
            frame_name = image["file_name"]
            unresolved_source = Path(frame_name)
            source_path = (session_dir / unresolved_source).resolve()
            if unresolved_source.is_absolute() or not source_path.is_relative_to(resolved_session_dir):
                raise DatasetFormatError(f"image file_name must remain within session: {frame_name}")
            persons = annotation_map.get(source_index, {}).get("persons", [])
            if not isinstance(persons, list):
                raise DatasetFormatError(f"persons must be a list for image {source_index}")
            staged.append((session_dir.name, frame_name, source_path, annotation_paths[0], persons))
    staged.sort(key=lambda item: (natural_sort_key(item[0]), natural_sort_key(item[1])))
    records: list[PersonRecord] = []
    person_index = 0
    for image_index, (session_name, frame_name, source_path, annotation_path, persons) in enumerate(staged, start=1):
        for person in persons:
            if not isinstance(person, dict):
                raise DatasetFormatError(f"person entry must be an object in {frame_name}")
            person_index += 1
            records.append(
                PersonRecord(
                    session_name,
                    frame_name,
                    source_path,
                    image_index,
                    person_index,
                    person.get("id", person_index),
                    _coerce_keypoints(person.get("keypoints"), str(annotation_path)),
                )
            )
    return DatasetIndex(len(staged), person_index, tuple(records))


# Deliberately not the same edge list as python/water_entry/common.py's SKELETON,
# and not shared with it: this page draws annotation ground truth, where
# visibility is COCO's integer v bit, while water_entry draws model output and
# gates on a float confidence. That module also adds nose-to-shoulder edges to
# make the head pose readable mid-dive. Merging the two would change one page's
# appearance for the other's reason.
COCO17_EDGES = ((0, 1), (0, 2), (1, 3), (2, 4), (5, 6), (5, 7), (7, 9), (6, 8), (8, 10), (5, 11), (6, 12), (11, 12), (11, 13), (13, 15), (12, 14), (14, 16))
SKELETON_COLOR = (0, 220, 255)
KEYPOINT_COLOR = (255, 230, 0)
EXACT_BOX_COLOR = (0, 60, 255)


@dataclass(frozen=True)
class GenerationSummary:
    source_image_count: int
    source_person_count: int
    generated_count: int
    skipped_count: int


def render_person_crop(
    image: np.ndarray,
    keypoints: Sequence[tuple[float, float, int]],
    exact: Rect,
    crop: Rect,
) -> np.ndarray:
    image_height, image_width = image.shape[:2]
    floor_left = int(math.floor(crop.left))
    floor_top = int(math.floor(crop.top))
    ceil_right = int(math.ceil(crop.right))
    ceil_bottom = int(math.ceil(crop.bottom))
    side = min(max(ceil_right - floor_left, ceil_bottom - floor_top), image_width, image_height)
    left = min(max(floor_left, 0), image_width - side)
    top = min(max(floor_top, 0), image_height - side)
    right = left + side
    bottom = top + side
    cropped = image[top:bottom, left:right].copy()

    def in_crop(point: tuple[float, float, int]) -> bool:
        x, y, visibility = point
        return visibility > 0 and math.isfinite(x) and math.isfinite(y)

    def to_crop_coordinates(x: float, y: float) -> tuple[int, int]:
        return (int(round(x - left)), int(round(y - top)))

    for start_index, end_index in COCO17_EDGES:
        if start_index >= len(keypoints) or end_index >= len(keypoints):
            continue
        start_point = keypoints[start_index]
        end_point = keypoints[end_index]
        if not in_crop(start_point) or not in_crop(end_point):
            continue
        cv2.line(cropped, to_crop_coordinates(start_point[0], start_point[1]), to_crop_coordinates(end_point[0], end_point[1]), SKELETON_COLOR, 2)

    for point in keypoints:
        if not in_crop(point):
            continue
        center = to_crop_coordinates(point[0], point[1])
        cv2.circle(cropped, center, 4, KEYPOINT_COLOR, -1)
        cv2.circle(cropped, center, 4, (0, 0, 0), 1)

    exact_top_left = to_crop_coordinates(exact.left, exact.top)
    exact_bottom_right = to_crop_coordinates(exact.right, exact.bottom)
    cv2.rectangle(cropped, exact_top_left, exact_bottom_right, EXACT_BOX_COLOR, 2)

    return cropped


def _escape_html(value: str) -> str:
    # quote=True escapes " as well, which matters because these values land inside
    # attribute values (src, alt).
    return escape(value, quote=True)


def _render_card_html(card: dict[str, object], card_index: int) -> str:
    image_source = _escape_html(str(card["image"]))
    alt_text = _escape_html(str(card["alt"]))
    metadata_text = _escape_html(str(card["metadata"]))
    return f"""    <div class=\"card\" data-card-index=\"{card_index}\">
      <img loading=\"lazy\" decoding=\"async\" src=\"{image_source}\" alt=\"{alt_text}\" data-src=\"{image_source}\">
      <div class=\"meta\">{metadata_text}</div>
    </div>"""


def _json_for_script(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _render_index_html(cards: list[dict[str, object]], summary: GenerationSummary) -> str:
    cards_json = _json_for_script(cards)
    cards_html = "\n".join(_render_card_html(card, card_index) for card_index, card in enumerate(cards))
    return f"""<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
<meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>Keypoint Preview</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #121212; color: #f0f0f0; }}
  header {{ position: sticky; top: 0; z-index: 10; background: #1b1b1b; padding: 12px 20px; border-bottom: 1px solid #333; }}
  header h1 {{ margin: 0 0 6px; font-size: 18px; }}
  header p {{ margin: 0; font-size: 13px; color: #ccc; }}
  .legend {{ display: flex; gap: 16px; margin-top: 8px; font-size: 12px; color: #ddd; }}
  .legend span {{ display: inline-flex; align-items: center; gap: 6px; }}
  .swatch {{ width: 12px; height: 12px; border-radius: 2px; display: inline-block; }}
  .swatch.exact {{ background: rgb(255, 60, 0); }}
  .swatch.skeleton {{ background: rgb(255, 220, 0); }}
  main {{ padding: 16px 20px 40px; }}
  .grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }}
  @media (max-width: 1100px) {{ .grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
  @media (max-width: 620px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .card {{ background: #1e1e1e; border-radius: 8px; overflow: hidden; border: 1px solid #2c2c2c; }}
  .card img {{ display: block; width: 100%; aspect-ratio: 1 / 1; object-fit: cover; background: #0d0d0d; }}
  .card .meta {{ padding: 8px 10px; font-size: 12px; color: #bbb; }}
  .card.load-failed .meta {{ color: #ff6b6b; }}
</style>
</head>
<body>
<header>
  <h1>Keypoint Preview</h1>
  <p>图片数 {summary.source_image_count} · 人物数 {summary.source_person_count} · 生成 {summary.generated_count} · 跳过 {summary.skipped_count}</p>
  <div class=\"legend\">
    <span><span class=\"swatch exact\"></span>红框：精准关键点框</span>
    <span><span class=\"swatch skeleton\"></span>骨架与关键点</span>
  </div>
</header>
<main>
  <div class=\"grid\" id=\"card-grid\">
{cards_html}
  </div>
</main>
<script>
  // Not python.common.page's lazy_loader: this page also marks cards whose image
  // fails to decode, and keeps a no-IntersectionObserver fallback because its
  // <img> already carries a real src (native loading="lazy"), so the swap here is
  // belt-and-braces rather than the only path.
  const cards = {cards_json};
  const grid = document.getElementById("card-grid");

  const loadImage = (image) => {{
    image.src = image.dataset.src;
    image.removeAttribute("data-src");
  }};
  const observer = "IntersectionObserver" in window
    ? new IntersectionObserver((entries, activeObserver) => {{
        entries.filter((entry) => entry.isIntersecting).forEach((entry) => {{
          loadImage(entry.target);
          activeObserver.unobserve(entry.target);
        }});
      }}, {{ rootMargin: "600px 0px" }})
    : null;

  Array.from(grid.querySelectorAll(".card")).forEach((cardElement, cardIndex) => {{
    const image = cardElement.querySelector("img");
    const card = cards[cardIndex];
    image.addEventListener("error", () => {{
      cardElement.classList.add("load-failed");
    }});

    if (observer) {{
      observer.observe(image);
    }} else {{
      loadImage(image);
    }}
  }});
</script>
</body>
</html>
"""


def _skip_record(record: PersonRecord, reason: str) -> dict[str, object]:
    return {
        "person_index": record.person_index,
        "image_index": record.image_index,
        "session_name": record.session_name,
        "frame_name": record.frame_name,
        "person_id": record.person_id,
        "reason": reason,
    }


def _publish_output(staging_dir: Path, output_dir: Path) -> None:
    backup_dir: Path | None = None
    try:
        if output_dir.exists():
            backup_path = tempfile.mkdtemp(prefix=f".{output_dir.name}.backup.", dir=output_dir.parent)
            backup_dir = Path(backup_path)
            backup_dir.rmdir()
            output_dir.replace(backup_dir)
        staging_dir.replace(output_dir)
    except BaseException:
        if not output_dir.exists() and backup_dir is not None and backup_dir.exists():
            backup_dir.replace(output_dir)
        raise
    else:
        if backup_dir is not None and backup_dir.exists():
            shutil.rmtree(backup_dir)


def generate_preview(
    dataset_root: Path,
    output_dir: Path,
    padding_ratio: float = 0.60,
    minimum_side: int = 160,
) -> GenerationSummary:
    _validate_crop_parameters(padding_ratio, minimum_side)
    resolved_dataset_root = dataset_root.resolve()
    resolved_output_dir = output_dir.resolve()
    if resolved_output_dir == resolved_dataset_root or resolved_output_dir.is_relative_to(resolved_dataset_root):
        raise ValueError("output directory must be outside dataset root")

    dataset = discover_dataset(dataset_root)
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_path = tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent)
    staging_dir = Path(staging_path)
    crops_dir = staging_dir / "crops"
    crops_dir.mkdir()
    try:
        cards = []
        skipped = []
        current_source_path: Path | None = None
        current_image: np.ndarray | None = None
        for record in dataset.records:
            points = visible_keypoints(record.keypoints)
            exact = exact_box(points)
            if exact is None:
                skipped.append(_skip_record(record, "no_visible_keypoints"))
                continue
            if record.source_path != current_source_path:
                current_source_path = record.source_path
                try:
                    current_image = cv2.imread(str(record.source_path), cv2.IMREAD_COLOR)
                except cv2.error:
                    current_image = None
            if current_image is None:
                skipped.append(_skip_record(record, "unreadable_source_image"))
                continue
            crop = square_crop_box(exact, current_image.shape[1], current_image.shape[0], padding_ratio, minimum_side)
            rendered = render_person_crop(current_image, record.keypoints, exact, crop)
            filename = f"{record.person_index:04d}.jpg"
            destination = crops_dir / filename
            try:
                written = cv2.imwrite(str(destination), rendered, [cv2.IMWRITE_JPEG_QUALITY, 92])
            except cv2.error as exc:
                raise OSError(f"cannot write preview crop: {destination}") from exc
            if not written:
                raise OSError(f"cannot write preview crop: {destination}")
            cards.append({"image": f"crops/{filename}", "alt": f"{record.session_name} {record.frame_name} person {record.person_id}", "metadata": f"图 {record.image_index}/{dataset.image_count} · 人 {record.person_index}/{dataset.person_count} · {record.session_name} / {record.frame_name} · ID {record.person_id}"})
        summary = GenerationSummary(dataset.image_count, dataset.person_count, len(cards), len(skipped))
        (staging_dir / "index.html").write_text(_render_index_html(cards, summary), encoding="utf-8")
        report = {
            "summary": {
                "source_image_count": summary.source_image_count,
                "source_person_count": summary.source_person_count,
                "generated_count": summary.generated_count,
                "skipped_count": summary.skipped_count,
            },
            "skipped": skipped,
        }
        (staging_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        _publish_output(staging_dir, output_dir)
        return summary
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
