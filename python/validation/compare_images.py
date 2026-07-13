"""Offline numeric and visual comparison for a Metal diagnostic render."""

import argparse
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np


LOGICAL_WIDTH = 5001
LOGICAL_HEIGHT = 2101
ENCODED_WIDTH = 5002
ENCODED_HEIGHT = 2102
MIN_PSNR = 48.0
MIN_SSIM = 0.9995
MAX_LOCAL_MAE = 1.25
MAX_LOCAL_RMSE = 3.75
DIFFERENCE_AMPLIFICATION = 4.0

LOCAL_REGIONS = {
    "center": (slice(1050, 1051), slice(0, LOGICAL_WIDTH)),
    "last_row": (slice(2100, 2101), slice(0, LOGICAL_WIDTH)),
    "last_column": (slice(0, LOGICAL_HEIGHT), slice(5000, 5001)),
}


def compute_global_ssim(a, b):
    c1, c2 = 6.5025, 58.5225
    mu_a, mu_b = float(a.mean()), float(b.mean())
    var_a, var_b = float(a.var()), float(b.var())
    covariance = float(((a - mu_a) * (b - mu_b)).mean())
    return (
        (2 * mu_a * mu_b + c1)
        * (2 * covariance + c2)
        / (
            (mu_a * mu_a + mu_b * mu_b + c1)
            * (var_a + var_b + c2)
        )
    )


def _read_color(path):
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"cannot read image: {path}")
    return image.astype(np.float32)


def _read_candidate(path):
    image = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if image is None:
        raise ValueError(f"cannot read image: {path}")
    if image.ndim != 3 or image.shape[2] not in (3, 4):
        raise ValueError("candidate must be an RGB or RGBA image")
    height, width = image.shape[:2]
    if (height, width) != (ENCODED_HEIGHT, ENCODED_WIDTH):
        raise ValueError(
            "candidate dimensions must be "
            f"{ENCODED_WIDTH}x{ENCODED_HEIGHT}, got {width}x{height}"
        )

    right_padding = image[:, LOGICAL_WIDTH]
    bottom_padding = image[LOGICAL_HEIGHT]
    if np.any(right_padding[:, :3] != 0) or np.any(
        bottom_padding[:, :3] != 0
    ):
        raise ValueError("candidate padding must be exactly black")
    if image.shape[2] == 4 and (
        np.any(right_padding[:, 3] != 255)
        or np.any(bottom_padding[:, 3] != 255)
    ):
        raise ValueError("candidate padding must be exactly opaque")
    return image


def _comparison_arrays(reference, candidate):
    a = _read_color(reference)
    if a.shape != (LOGICAL_HEIGHT, LOGICAL_WIDTH, 3):
        raise ValueError(
            "reference dimensions must be "
            f"{LOGICAL_WIDTH}x{LOGICAL_HEIGHT}, got "
            f"{a.shape[1]}x{a.shape[0]}"
        )
    encoded = _read_candidate(candidate)
    b = encoded[:LOGICAL_HEIGHT, :LOGICAL_WIDTH, :3].astype(np.float32)
    return a, b


def _error_metrics(difference):
    absolute = np.abs(difference)
    return {
        "mae": float(absolute.mean()),
        "rmse": float(np.sqrt(np.mean(difference * difference))),
    }


def compare(reference, candidate):
    a, b = _comparison_arrays(reference, candidate)
    difference = a - b
    mse = float(np.mean(difference * difference))
    psnr = (
        float("inf")
        if mse == 0
        else float(20 * np.log10(255) - 10 * np.log10(mse))
    )
    ssim = compute_global_ssim(a, b)
    local = {
        name: _error_metrics(difference[region])
        for name, region in LOCAL_REGIONS.items()
    }
    duplicate_last_row = not np.array_equal(
        a[2100, :5000], a[2099, :5000]
    ) and np.array_equal(b[2100, :5000], b[2099, :5000])
    duplicate_last_column = not np.array_equal(
        a[:2100, 5000], a[:2100, 4999]
    ) and np.array_equal(b[:2100, 5000], b[:2100, 4999])
    return {
        "psnr": psnr,
        "ssim": ssim,
        "local": local,
        "duplicates": {
            "last_row": duplicate_last_row,
            "last_column": duplicate_last_column,
        },
    }


def passes_acceptance(metrics):
    if metrics["psnr"] < MIN_PSNR or metrics["ssim"] < MIN_SSIM:
        return False
    if any(metrics["duplicates"].values()):
        return False
    return all(
        region["mae"] <= MAX_LOCAL_MAE
        and region["rmse"] <= MAX_LOCAL_RMSE
        for region in metrics["local"].values()
    )


def difference_path(candidate):
    candidate = Path(candidate)
    return candidate.with_name(f"{candidate.stem}_diff.png")


def write_amplified_difference(reference, candidate):
    a, b = _comparison_arrays(reference, candidate)
    difference = np.clip(
        np.abs(a - b) * DIFFERENCE_AMPLIFICATION, 0, 255
    ).astype(np.uint8)
    output = difference_path(candidate)
    if not cv2.imwrite(str(output), difference):
        raise ValueError(f"cannot write difference image: {output}")
    return output


def run_cli(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    args = parser.parse_args(argv)

    try:
        metrics = compare(args.reference, args.candidate)
        output = write_amplified_difference(args.reference, args.candidate)
    except ValueError as error:
        parser.error(str(error))
    print(
        f"PSNR={metrics['psnr']:.6f} SSIM={metrics['ssim']:.9f} "
        f"center_MAE={metrics['local']['center']['mae']:.6f} "
        f"center_RMSE={metrics['local']['center']['rmse']:.6f} "
        f"last_row_MAE={metrics['local']['last_row']['mae']:.6f} "
        f"last_row_RMSE={metrics['local']['last_row']['rmse']:.6f} "
        f"last_column_MAE={metrics['local']['last_column']['mae']:.6f} "
        f"last_column_RMSE={metrics['local']['last_column']['rmse']:.6f} "
        f"last_row_duplicate={metrics['duplicates']['last_row']} "
        f"last_column_duplicate={metrics['duplicates']['last_column']} "
        f"diff={output}"
    )
    return int(not passes_acceptance(metrics))


def main():
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
