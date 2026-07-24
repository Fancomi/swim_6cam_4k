#!/usr/bin/env python3
"""补齐缺帧：underA5/f14 图像损坏，用相邻 f13、f15 的同序号点线性插值。

f14 为二者正中间帧，按 y 排序后逐点取均值。写回前备份工程。
"""
import json
import shutil

from python.annotation_preview import common as C


def main():
    doc = C.load_project()
    groups = C.group_by_camera(doc)
    cols = {fi: sorted(pts, key=lambda q: q["y"]) for fi, _r, pts in groups["underA5"]}
    f13, f15 = cols.get(13), cols.get(15)
    target = next(im for im in doc["images"] if "underA5/f14_" in im["image"])
    if len(f13) != C.N_ROWS or len(f15) != C.N_ROWS:
        raise SystemExit("邻帧点数异常，无法插值")
    if target["points"]:
        print("underA5/f14 已有 %d 点，跳过。" % len(target["points"]))
        return
    interp = [{"x": round((a["x"] + b["x"]) / 2, 2),
               "y": round((a["y"] + b["y"]) / 2, 2)} for a, b in zip(f13, f15)]
    shutil.copy2(C.PROJECT_JSON, C.PROJECT_JSON + ".bak")
    target["points"] = interp
    target["interpolated"] = True
    json.dump(doc, open(C.PROJECT_JSON, "w"), ensure_ascii=False, indent=2)
    print("interpolated underA5/f14 -> %d points (backup: *.bak)" % len(interp))
    for i, p in enumerate(interp):
        print("  R%d  x=%.1f  y=%.1f" % (i + 1, p["x"], p["y"]))


if __name__ == "__main__":
    main()
