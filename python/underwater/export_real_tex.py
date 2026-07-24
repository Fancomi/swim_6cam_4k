"""Dump each camera's first frame (no grid overlay) as a stitch texture.

The textures baked into all.fbm are annotation-grid overlays; the "原图像" the
stitch should use is each camera's FIRST captured frame. Every camera's first
frame lives in the dataset (earliest snapshot, all 16 cameras present). This
exporter writes each camera's first frame under the exact basename the mesh JSON
references (`underAi-grid.png`), so render.py can stitch photographic imagery by
only swapping --tex-dir.

Reuses annotation_preview.common for dataset paths and per-camera frame
enumeration (which is time-ordered, so element 0 is the first frame).
"""
import argparse
from pathlib import Path

from PIL import Image

from python.annotation_preview import common as C

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUTS_DIR = PROJECT_ROOT / "outputs"


def first_frame_path(cam):
    """Absolute path to `cam`'s first (earliest snapshot) frame, or None.

    C.frames_for_camera returns [(snapshot_id, path)] sorted by snapshot time,
    so element 0 is the first frame captured for that camera."""
    frames = C.frames_for_camera(cam)
    return frames[0][1] if frames else None


def export(out_dir, cams=None):
    out_dir = Path(out_dir)
    if not Path(C.SNAP_DIR).is_dir():
        raise SystemExit(
            f"缺少快照目录：{C.SNAP_DIR}"
            "（请通过 ANNOTATION_PREVIEW_DATASET_ROOT 指向有效数据集）")
    cams = cams or C.CAMS_ASC
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for cam in cams:
        src = first_frame_path(cam)
        if src is None:
            print(f"  {cam:9s} (no frames — skipped)")
            continue
        # keep the mesh JSON's basename so render.py finds it via --tex-dir
        dst = out_dir / f"{cam}-grid.png"
        Image.open(src).convert("RGB").save(dst)
        written.append(dst)
        print(f"  {cam:9s} <- {Path(src).name}")
    if not written:
        raise SystemExit("no first frames exported")
    print(f"wrote {len(written)} first-frame textures -> {out_dir}")
    return written


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export first-frame textures")
    ap.add_argument("--out-dir", type=Path,
                    default=OUTPUTS_DIR / "underwater" / "real_tex_all")
    args = ap.parse_args(argv)
    export(args.out_dir)


if __name__ == "__main__":
    main()
