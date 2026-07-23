#!/usr/bin/env python3
"""按 detections.csv 把有物体帧原图导出到 object-frames/<camera>/，附参考 csv。

只读源快照，只写 object-frames/。文件名前缀 f<帧号> 保证有序唯一。
"""
import csv
import os
import shutil
import common as C

DET_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "detections.csv")


def main():
    rows = list(csv.DictReader(open(DET_CSV)))
    obj = [r for r in rows if r["is_object"] == "1"]
    os.makedirs(C.OBJ_DIR, exist_ok=True)
    per_cam, missing = {}, []
    for r in obj:
        src = os.path.join(C.SNAP_DIR, r["snapshot_id"], r["filename"])
        if not os.path.exists(src):
            missing.append(src)
            continue
        cam_dir = os.path.join(C.OBJ_DIR, r["camera"])
        os.makedirs(cam_dir, exist_ok=True)
        dst = os.path.join(cam_dir, "f%02d_%s__%s"
                           % (int(r["frame_index"]), r["snapshot_id"], r["filename"]))
        shutil.copy2(src, dst)
        per_cam[r["camera"]] = per_cam.get(r["camera"], 0) + 1

    with open(os.path.join(C.OBJ_DIR, "detections.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(obj)

    print("exported %d object frames -> %s" % (sum(per_cam.values()), C.OBJ_DIR))
    for cam in C.CAMS_ASC:
        if cam in per_cam:
            print("  %-8s %d" % (cam, per_cam[cam]))
    if missing:
        print("MISSING %d source files, e.g. %s" % (len(missing), missing[0]))


if __name__ == "__main__":
    main()
