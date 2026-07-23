#!/usr/bin/env python3
"""逐相机中值帧背景差检测：输出 detections.csv 供复核（不导图）。

分数 = 与中值帧颜色距离 > DIST_THRESH 的像素占比；
自适应阈值 = 该相机分数中位数 * MULT，超阈判为有物体帧。
"""
import csv
import os
import numpy as np
import common as C

OUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "detections.csv")
MULT = 1.8
COLS = ["camera", "frame_index", "snapshot_id", "score_frac_gt40",
        "cam_median", "threshold", "is_object", "filename"]


def analyze(cam):
    fr = C.frames_for_camera(cam)
    _median, dist = C.median_dist([p for _, p in fr])
    scores = (dist > C.DIST_THRESH).mean(axis=(1, 2))
    med = float(np.median(scores))
    thr = med * MULT
    return [{
        "camera": cam, "frame_index": i + 1, "snapshot_id": sid,
        "score_frac_gt40": round(float(scores[i]), 5),
        "cam_median": round(med, 5), "threshold": round(thr, 5),
        "is_object": int(scores[i] > thr), "filename": os.path.basename(path),
    } for i, (sid, path) in enumerate(fr)]


def main():
    rows = []
    for cam in C.CAMS_ASC:
        r = analyze(cam)
        print("%-8s frames=%d  median=%.4f  thr=%.4f  objects=%d"
              % (cam, len(r), r[0]["cam_median"], r[0]["threshold"],
                 sum(x["is_object"] for x in r)))
        rows.extend(r)
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerows(rows)
    print("\nTOTAL object frames: %d / %d  ->  %s"
          % (sum(r["is_object"] for r in rows), len(rows), OUT_CSV))


if __name__ == "__main__":
    main()
