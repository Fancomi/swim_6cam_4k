#!/usr/bin/env python3
"""按 detections.csv 把帧原图导出到 <out-dir>/<camera>/，附参考 csv。

默认只导出有物体帧（is_object==1）到 object-frames/。调试时用 --all 关闭筛选，
导出全部帧（默认落到相邻目录 object-frames-all/），便于查看被筛掉的帧。
只读源快照，只写目标目录。文件名前缀 f<帧号> 保证有序唯一。
"""
import argparse
import csv
import os
import shutil

from python.annotation_preview import common as C

DET_CSV = os.path.join(C.OUTPUT_ROOT, "detections.csv")
# 生成产物统一落在 outputs/annotation_preview 下（可用 --out-dir 覆盖）。
OBJ_OUT_DIR = os.path.join(C.OUTPUT_ROOT, "object-frames")
ALL_DIR = os.path.join(C.OUTPUT_ROOT, "object-frames-all")


def export(rows, out_dir, keep_all):
    selected = rows if keep_all else [r for r in rows if r["is_object"] == "1"]
    os.makedirs(out_dir, exist_ok=True)
    per_cam, missing = {}, []
    for r in selected:
        src = os.path.join(C.SNAP_DIR, r["snapshot_id"], r["filename"])
        if not os.path.exists(src):
            missing.append(src)
            continue
        cam_dir = os.path.join(out_dir, r["camera"])
        os.makedirs(cam_dir, exist_ok=True)
        dst = os.path.join(cam_dir, "f%02d_%s__%s"
                           % (int(r["frame_index"]), r["snapshot_id"], r["filename"]))
        shutil.copy2(src, dst)
        per_cam[r["camera"]] = per_cam.get(r["camera"], 0) + 1

    with open(os.path.join(out_dir, "detections.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(selected)
    return per_cam, missing, len(selected)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true",
                    help="导出全部帧（不按 is_object 筛选），调试用")
    ap.add_argument("--out-dir", default=None,
                    help="输出目录；默认 outputs/annotation_preview/object-frames，"
                         "--all 时 object-frames-all")
    args = ap.parse_args()

    out_dir = args.out_dir or (ALL_DIR if args.all else OBJ_OUT_DIR)
    if not os.path.exists(DET_CSV):
        raise SystemExit(
            "缺少 detections.csv：%s（请先运行 detect_objects 生成）" % DET_CSV)
    rows = list(csv.DictReader(open(DET_CSV)))
    per_cam, missing, n = export(rows, out_dir, args.all)

    mode = "ALL frames (unfiltered)" if args.all else "object frames"
    print("exported %d %s -> %s" % (n, mode, out_dir))
    for cam in C.CAMS_ASC:
        if cam in per_cam:
            print("  %-8s %d" % (cam, per_cam[cam]))
    if missing:
        print("MISSING %d source files, e.g. %s" % (len(missing), missing[0]))


if __name__ == "__main__":
    main()
