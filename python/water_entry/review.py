#!/usr/bin/env python3
"""把预测结果画成可复核的物证：逐帧骨架叠加图 + 单页 HTML 复核页。

对每个片段的每个模型，取「入水帧 ±RADIUS」这段最关键的帧（README §3.2：
触水前后各 3 帧是现网与通用模型最不稳的地方），把选中目标的框、COCO-17 骨架、
肩/胯中点与入水判据的 shoulder_y-hip_y 数值烧进图里，缺检的帧标红 MISS。
同一帧的多个模型横向拼成一行，便于直接对比。

依赖 predict.py 已写出的 per_frame/<clip>.json；不重新推理。
产物：outputs/water_entry/review/{crops/,index.html}
"""
import argparse
import glob
import html
import json
import os

import cv2
import numpy as np

from python.water_entry import common as C

BOX_COLOR = (60, 220, 60)
BONE_COLOR = (0, 220, 255)
KP_COLOR = (0, 90, 255)
MID_SHO = (255, 120, 0)
MID_HIP = (255, 0, 200)


def draw_overlay(image, rec):
    """在帧副本上画出选中目标的框、骨架与肩胯中点（不画文字，避免被裁掉）。"""
    canvas = image.copy()
    if rec.get("kps_xy") is None:
        return canvas
    x1, y1, x2, y2 = (int(round(v)) for v in rec["box"])
    cv2.rectangle(canvas, (x1, y1), (x2, y2), BOX_COLOR, 2)
    xy, cf = rec["kps_xy"], rec["kps_conf"]
    for a, b in C.SKELETON:
        if cf[a] >= C.KP_CONF and cf[b] >= C.KP_CONF:
            cv2.line(canvas, (int(xy[a][0]), int(xy[a][1])),
                     (int(xy[b][0]), int(xy[b][1])), BONE_COLOR, 2)
    for (px, py), c in zip(xy, cf):
        if c >= C.KP_CONF:
            cv2.circle(canvas, (int(px), int(py)), 3, KP_COLOR, -1)
    sho = C.midpoint(xy, cf, C.L_SHO, C.R_SHO)
    hip = C.midpoint(xy, cf, C.L_HIP, C.R_HIP)
    if sho:
        cv2.circle(canvas, (int(sho[0]), int(sho[1])), 6, MID_SHO, -1)
    if hip:
        cv2.circle(canvas, (int(hip[0]), int(hip[1])), 6, MID_HIP, -1)
    if sho and hip:
        cv2.line(canvas, (int(sho[0]), int(sho[1])),
                 (int(hip[0]), int(hip[1])), (255, 255, 255), 1)
    return canvas


def draw_caption(canvas, rec, label):
    """裁剪之后再烧文字：标题、缺检标记、入水判据数值与置信度。

    先压一条半透明黑带，否则白字压在亮池水上读不出来。
    """
    lines = [(label, (255, 255, 255))]
    if rec.get("kps_xy") is None:
        lines.append(("MISS", (0, 0, 255)))
    else:
        sho = C.midpoint(rec["kps_xy"], rec["kps_conf"], C.L_SHO, C.R_SHO)
        hip = C.midpoint(rec["kps_xy"], rec["kps_conf"], C.L_HIP, C.R_HIP)
        if sho and hip:
            lines.append(("sho-hip %+.0f" % (sho[1] - hip[1]), (255, 255, 255)))
        lines.append(("conf %.2f  n=%d" % (rec["conf"], rec["n_det"]), BOX_COLOR))

    band = 12 + 26 * len(lines)
    strip = canvas[:band].copy()
    cv2.rectangle(strip, (0, 0), (canvas.shape[1], band), (0, 0, 0), -1)
    canvas[:band] = cv2.addWeighted(strip, 0.55, canvas[:band], 0.45, 0)
    for i, (text, color) in enumerate(lines):
        cv2.putText(canvas, text, (10, 26 + 26 * i), cv2.FONT_HERSHEY_SIMPLEX,
                    0.62, color, 2)
    return canvas


def crop_around(image, centre, side):
    """以给定中心裁出正方形，越界时贴边。"""
    h, w = image.shape[:2]
    side = int(min(side, w, h))
    half = side / 2.0
    x0 = int(round(min(max(0, centre[0] - half), w - side)))
    y0 = int(round(min(max(0, centre[1] - half), h - side)))
    return image[y0:y0 + side, x0:x0 + side]


def frame_centre(by_model, frame, width, height):
    """该帧的共享裁剪中心：所有模型在此帧检出框中心的均值。

    同一行的各模型必须用同一个中心，否则「A 检出、B 缺检」两格画的不是同一块
    画面，无法判断 B 是真的漏检还是被裁到画外。全都缺检时退回画面中心。
    """
    centres = []
    for payload in by_model.values():
        rec = next((r for r in payload["frames"] if r["frame"] == frame), None)
        if rec and rec.get("box") is not None:
            centres.append(((rec["box"][0] + rec["box"][2]) / 2.0,
                            (rec["box"][1] + rec["box"][3]) / 2.0))
    if not centres:
        return (width / 2.0, height / 2.0)
    return (float(np.mean([c[0] for c in centres])),
            float(np.mean([c[1] for c in centres])))


def load_predictions(predict_dir, models, clips):
    """读取 per_frame JSON，返回 {clip: {model: payload}}，按片段名排序。"""
    found = {}
    for model in models:
        for path in sorted(glob.glob(os.path.join(predict_dir, model,
                                                  "per_frame", "*.json"))):
            name = os.path.splitext(os.path.basename(path))[0]
            if clips and name not in clips:
                continue
            with open(path) as f:
                found.setdefault(name, {})[model] = json.load(f)
    return dict(sorted(found.items()))


def build_clip_rows(name, by_model, models, radius, side, crops_dir, full):
    """为一个片段渲染裁剪图，返回 (entry_frame, [(frame, [(model, rel, miss)])])。"""
    any_payload = next(iter(by_model.values()))
    entry = any_payload["entry_frame"]
    lo, hi = entry - radius, entry + radius
    wanted = sorted({r["frame"] for p in by_model.values() for r in p["frames"]
                     if lo <= r["frame"] <= hi})
    clip = C.Clip(name=name, jump_frame=any_payload["jump_frame"],
                  water_frame=any_payload["manifest_water_frame"],
                  angle=0.0, backstroke_applied=False, note="")
    images = C.read_frames(clip.video, wanted)
    rows = []
    for frame in wanted:
        if frame not in images:
            continue
        height, width = images[frame].shape[:2]
        centre = frame_centre(by_model, frame, width, height)
        cells = []
        for model in models:
            payload = by_model.get(model)
            if payload is None:
                continue
            rec = next((r for r in payload["frames"] if r["frame"] == frame),
                       {"frame": frame, "n_det": 0, "box": None, "conf": None,
                        "kps_xy": None, "kps_conf": None})
            drawn = draw_overlay(images[frame], rec)
            if not full:
                drawn = crop_around(drawn, centre, side).copy()
            drawn = draw_caption(drawn, rec, "%s f%d" % (model, frame))
            rel = os.path.join(name, "%s_f%03d.jpg" % (model, frame))
            out = os.path.join(crops_dir, rel)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            cv2.imwrite(out, drawn, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            cells.append((model, rel, rec["kps_xy"] is None))
        rows.append((frame, cells))
    return entry, rows


PAGE_CSS = """
body{background:#14161a;color:#dde3ea;font:13px/1.5 -apple-system,Helvetica,sans-serif;margin:24px}
h1{font-size:19px;margin:0 0 4px}
h2{font-size:15px;margin:28px 0 6px;border-bottom:1px solid #2c313a;padding-bottom:4px}
.meta{color:#8d97a5}
.row{display:flex;gap:8px;align-items:flex-start;margin:8px 0}
.tag{min-width:64px;color:#8d97a5;padding-top:4px}
.tag.entry{color:#ffd166;font-weight:600}
figure{margin:0}
figure img{display:block;width:var(--w);border:2px solid #2c313a;border-radius:4px;background:#000}
figure.miss img{border-color:#e03131}
figcaption{color:#8d97a5;font-size:11px;text-align:center}
"""



def write_page(path, sections, models, radius, cell_width):
    """写单页 HTML：一片段一节，一行一帧，行内按模型横排。"""
    out = ["<!doctype html><html lang=\"zh\"><head><meta charset=\"utf-8\">",
           "<title>入水检测 pose 复核</title><style>", PAGE_CSS,
           ":root{--w:%dpx}" % cell_width, "</style></head><body>",
           "<h1>入水检测机位 YOLO-pose 复核</h1>",
           "<p class=\"meta\">模型横排：%s ｜ 每片段取入水帧 ±%d 帧 ｜ "
           "黄行为基准入水帧，红框为该模型缺检 ｜ 橙点=肩中点，粉点=胯中点，"
           "sho-hip 由负转正即入水判据</p>" % (html.escape(" / ".join(models)), radius)]
    for name, entry, rows, notes in sections:
        out.append("<h2>%s <span class=\"meta\">entry=%d ｜ %s</span></h2>"
                   % (html.escape(name), entry, html.escape(notes)))
        for frame, cells in rows:
            tag = "tag entry" if frame == entry else "tag"
            out.append("<div class=\"row\"><div class=\"%s\">f%d</div>" % (tag, frame))
            for model, rel, miss in cells:
                out.append("<figure class=\"%s\"><img data-src=\"crops/%s\" "
                           "alt=\"%s f%d\"><figcaption>%s</figcaption></figure>"
                           % ("miss" if miss else "", rel,
                              html.escape(model), frame, html.escape(model)))
            out.append("</div>")
    out.append("<script>%s</script></body></html>" % C.lazy_img_js(600))
    with open(path, "w") as f:
        f.write("\n".join(out))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--predict-dir", default=os.path.join(C.OUTPUT_ROOT, "predict"),
                    help="predict.py 的输出目录（默认 %(default)s）")
    ap.add_argument("--models", nargs="+", default=C.DEFAULT_MODELS,
                    help="要横向对比的模型子目录名，顺序即页面列顺序")
    ap.add_argument("--clips", nargs="*", default=None, help="只出指定片段")
    ap.add_argument("--limit", type=int, default=8,
                    help="最多渲染前 N 个片段（0 = 全部，默认 %(default)s）")
    ap.add_argument("--radius", type=int, default=C.ENTRY_RADIUS,
                    help="取入水帧 ±RADIUS 帧（默认 %(default)s）")
    ap.add_argument("--side", type=int, default=420, help="正方形裁剪边长")
    ap.add_argument("--full-frame", action="store_true",
                    help="不裁剪，输出整帧（看池边干扰目标时用）")
    ap.add_argument("--cell-width", type=int, default=300, help="页面单元显示宽度 px")
    ap.add_argument("--output-dir", default=os.path.join(C.OUTPUT_ROOT, "review"))
    args = ap.parse_args()

    preds = load_predictions(args.predict_dir, args.models,
                             set(args.clips) if args.clips else None)
    if not preds:
        raise SystemExit("在 %s 下找不到 per_frame 结果，先运行 python -m "
                         "python.water_entry.predict" % args.predict_dir)
    names = list(preds)
    if args.limit:
        names = names[:args.limit]

    crops_dir = os.path.join(args.output_dir, "crops")
    os.makedirs(crops_dir, exist_ok=True)
    sections = []
    for i, name in enumerate(names, 1):
        by_model = preds[name]
        entry, rows = build_clip_rows(name, by_model, args.models, args.radius,
                                      args.side, crops_dir, args.full_frame)
        any_payload = next(iter(by_model.values()))
        misses = {m: sum(1 for _f, cells in rows
                         for mm, _r, miss in cells if mm == m and miss)
                  for m in args.models}
        notes = ("entry_source=%s ｜ manifest water_frame=%d ｜ 缺检 %s"
                 % (any_payload["entry_source"],
                    any_payload["manifest_water_frame"],
                    ", ".join("%s %d/%d" % (m, n, len(rows))
                              for m, n in misses.items())))
        sections.append((name, entry, rows, notes))
        print("[%d/%d] %s entry=%d rows=%d  %s"
              % (i, len(names), name, entry, len(rows), notes))

    page = os.path.join(args.output_dir, "index.html")
    write_page(page, sections, args.models, args.radius, args.cell_width)
    print("done -> %s" % page)


if __name__ == "__main__":
    main()
