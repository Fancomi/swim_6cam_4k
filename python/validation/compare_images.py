"""Offline numeric and visual comparison for a Metal diagnostic render."""

import argparse
from pathlib import Path
from typing import Sequence

import cv2
import numpy as np


MIN_PSNR = 45.0
MIN_SSIM = 0.995
DIFFERENCE_AMPLIFICATION = 4.0


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


def _comparison_arrays(reference, candidate):
    a = _read_color(reference)
    b = _read_color(candidate)
    cropped = b[: a.shape[0], : a.shape[1]]
    if a.shape != cropped.shape:
        raise ValueError(
            f"candidate is smaller than reference: {b.shape} vs {a.shape}"
        )
    return a, cropped


def compare(reference, candidate):
    a, b = _comparison_arrays(reference, candidate)
    mse = float(np.mean((a - b) ** 2))
    psnr = (
        float("inf")
        if mse == 0
        else float(20 * np.log10(255) - 10 * np.log10(mse))
    )
    ssim = compute_global_ssim(a, b)
    return {"psnr": psnr, "ssim": ssim}


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
        f"diff={output}"
    )
    return int(
        metrics["psnr"] < MIN_PSNR or metrics["ssim"] < MIN_SSIM
    )


def main():
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
