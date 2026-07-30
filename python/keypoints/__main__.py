"""CLI entry point for rendering the COCO-17 keypoint crop review page."""

import argparse
from pathlib import Path

from python.common.paths import OUTPUTS, dataset_root
from python.keypoints.preview import DatasetFormatError, generate_preview

DEFAULT_DATASET_ROOT = dataset_root(
    "SWIM_KEYPOINT_DATASET_ROOT",
    "/Users/penghaotian/Downloads/DATAS/SWIMMING/swim-6cam-4k/swim-6cam-4k-2dkp")
DEFAULT_OUTPUT_DIR = OUTPUTS / "keypoints"


def main() -> None:
    parser = argparse.ArgumentParser(description="Render person-level COCO-17 crops and a lazy-loading HTML review page.")
    parser.add_argument("--dataset-root", type=Path, default=DEFAULT_DATASET_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--padding-ratio", type=float, default=0.60)
    parser.add_argument("--minimum-side", type=int, default=160)
    args = parser.parse_args()
    try:
        summary = generate_preview(args.dataset_root, args.output_dir, args.padding_ratio, args.minimum_side)
    except (DatasetFormatError, OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"source images: {summary.source_image_count}")
    print(f"source persons: {summary.source_person_count}")
    print(f"generated crops: {summary.generated_count}")
    print(f"skipped crops: {summary.skipped_count}")
    print(f"open: {args.output_dir / 'index.html'}")


if __name__ == "__main__":
    main()
