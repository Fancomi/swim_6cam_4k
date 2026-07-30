#!/usr/bin/env python3
"""把 select_frames 挑出的候选帧渲染成标注前的质检页。

每个候选帧一行：左格 swimup 叠加、中格 swimup_bk 叠加、右格无叠加原图（标注员实际
要看的画面）。行首列出命中的信号与分数，便于判断「这一帧到底值不值得花标注预算」。

复用 review.py 的绘制与裁剪函数，不重新推理。
产物：outputs/water_entry/annotate_preview/{crops/,index.html}
"""
import argparse
import json
import os

import cv2

from python.common.media import read_frames, write_image
from python.common.page import escape, write_page as write_html
from python.common.tables import read_rows
from python.water_entry import common as C
from python.water_entry.review import (crop_around, draw_caption, draw_overlay,
                                       shared_centre)
from python.water_entry.select_frames import MODEL_A, MODEL_B

REASON_LABEL = {
    "both_blind": "两模型都 0 检出",
    "both_reject": "有检出但都没选中",
    "one_miss": "仅一个模型检出",
    "diff_person": "两模型指向不同的人",
    "kp_disagree": "关键点分歧大",
    "torso_broken": "躯干四点不全",
    "sign_flip": "肩胯上下判断相反",
}
PHASE_LABEL = {"pre": "起跳前", "flight": "飞行段", "entry": "入水±3帧", "post": "入水后"}


def load_candidates(path, limit):
    rows = read_rows(path)
    return rows[:limit] if limit else rows


def load_frame_records(predict_dir, clip):
    """读取两个模型该片段的逐帧记录，返回 (payload_a, by_frame_a, by_frame_b)。"""
    payloads = {}
    for model in (MODEL_A, MODEL_B):
        with open(os.path.join(predict_dir, model, "per_frame",
                              clip + ".json")) as f:
            payloads[model] = json.load(f)
    return (payloads[MODEL_A],
            {r["frame"]: r for r in payloads[MODEL_A]["frames"]},
            {r["frame"]: r for r in payloads[MODEL_B]["frames"]})


def empty_record(frame):
    return {"frame": frame, "n_det": 0, "box": None, "conf": None,
            "kps_xy": None, "kps_conf": None}


def render_row(row, predict_dir, crops_dir, side, full):
    """渲染一个候选帧的三格图，返回 (cells, 元信息字典)。"""
    clip, frame = row["clip"], int(row["frame"])
    payload, by_a, by_b = load_frame_records(predict_dir, clip)
    rec_a = by_a.get(frame, empty_record(frame))
    rec_b = by_b.get(frame, empty_record(frame))

    images = read_frames(os.path.join(C.CLIP_DIR, clip + ".mp4"), [frame])
    if frame not in images:
        return [], {}
    image = images[frame]
    height, width = image.shape[:2]
    centre = shared_centre((rec_a, rec_b), width, height)

    cells = []
    for label, rec in ((MODEL_A, rec_a), (MODEL_B, rec_b), ("raw", None)):
        canvas = image if rec is None else draw_overlay(image, rec)
        canvas = (canvas.copy() if full else crop_around(canvas, centre, side).copy())
        if rec is None:
            # 原图格只写标题：MISS 是模型状态，不该出现在「标注员看到的画面」上。
            cv2.putText(canvas, "raw f%d" % frame, (10, 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.62, (255, 255, 255), 2)
        else:
            canvas = draw_caption(canvas, rec, "%s f%d" % (label, frame))
        rel = os.path.join(clip, "f%03d_%s.jpg" % (frame, label))
        write_image(os.path.join(crops_dir, rel), canvas, "candidate crop")
        cells.append((label, rel))
    return cells, {"entry_frame": payload["entry_frame"],
                   "jump_frame": payload["jump_frame"],
                   "entry_source": payload["entry_source"]}


PAGE_CSS = """
.legend{margin:10px 0 20px;padding:10px 14px;background:#1b1e24;border-radius:6px}
.legend code{color:#ffd166}
.item{margin:18px 0;padding-top:10px;border-top:1px solid #2c313a}
.head{display:flex;gap:14px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px}
.head .clip{font-weight:600}
.head .score{color:#ffd166}
.head .reasons{color:#7bd88f}
.head .phase{color:#8d97a5}
.strip{display:flex;gap:8px}
figure.raw img{border-color:#4a5361}
"""


def write_page(path, items, cell_width, source_csv, total):
    body = ["<h1>增量标注候选帧质检</h1>",
            "<p class=\"meta\">来源 %s ｜ 本页 %d / 候选 %d 帧 ｜ "
            "三格依次为 swimup 叠加、swimup_bk 叠加、无叠加原图</p>"
            % (escape(os.path.basename(source_csv)), len(items), total),
            "<div class=\"legend\">信号含义："
            + " ｜ ".join("<code>%s</code> %s" % (k, v)
                         for k, v in REASON_LABEL.items())
            + "<br>橙点为肩中点、粉点为胯中点，<code>sho-hip</code> 由负转正即入水判据；"
              "<code>MISS</code> 表示该模型此帧没有选中目标。</div>"]
    for row, cells, info in items:
        body.append("<div class=\"item\"><div class=\"head\">")
        body.append("<span class=\"clip\">%s</span>" % escape(row["clip"]))
        body.append("<span>f%s（入水 f%d，偏移 %+d）</span>"
                    % (row["frame"], info["entry_frame"],
                       int(row["offset_to_entry"])))
        body.append("<span class=\"phase\">%s</span>"
                    % PHASE_LABEL.get(row["phase"], row["phase"]))
        body.append("<span class=\"reasons\">%s</span>"
                    % escape(" + ".join(REASON_LABEL.get(r, r)
                                        for r in row["reasons"].split("|"))))
        body.append("<span class=\"score\">score %s</span>" % row["score"])
        if row["note"]:
            body.append("<span class=\"meta\">note=%s</span>" % escape(row["note"]))
        body.append("</div><div class=\"strip\">")
        for label, rel in cells:
            body.append("<figure class=\"%s\"><img data-src=\"crops/%s\" alt=\"%s\">"
                        "<figcaption>%s</figcaption></figure>"
                        % ("raw" if label == "raw" else "", rel,
                           escape(label), escape(label)))
        body.append("</div></div>")
    return write_html(path, "增量标注候选帧质检", body, css=PAGE_CSS,
                      cell_width=cell_width, lazy_margin=700)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates",
                    default=os.path.join(C.OUTPUT_ROOT, "annotate_candidates.csv"),
                    help="select_frames 输出的 CSV（默认 %(default)s）")
    ap.add_argument("--predict-dir", default=os.path.join(C.OUTPUT_ROOT, "predict"))
    ap.add_argument("--limit", type=int, default=100,
                    help="只渲染前 N 帧（CSV 已按分数降序，默认 %(default)s；0 = 全部）")
    ap.add_argument("--side", type=int, default=420, help="正方形裁剪边长")
    ap.add_argument("--full-frame", action="store_true", help="不裁剪，输出整帧")
    ap.add_argument("--cell-width", type=int, default=300, help="单元显示宽度 px")
    ap.add_argument("--output-dir",
                    default=os.path.join(C.OUTPUT_ROOT, "annotate_preview"))
    args = ap.parse_args()

    if not os.path.exists(args.candidates):
        raise SystemExit("找不到候选 CSV：%s（先运行 python -m "
                         "python.water_entry.select_frames）" % args.candidates)
    total = len(read_rows(args.candidates))
    rows = load_candidates(args.candidates, args.limit)
    crops_dir = os.path.join(args.output_dir, "crops")
    os.makedirs(crops_dir, exist_ok=True)

    items = []
    for i, row in enumerate(rows, 1):
        cells, info = render_row(row, args.predict_dir, crops_dir,
                                 args.side, args.full_frame)
        if not cells:
            print("  跳过 %s f%s：视频里取不到该帧" % (row["clip"], row["frame"]))
            continue
        items.append((row, cells, info))
        if i % 20 == 0 or i == len(rows):
            print("  渲染 %d/%d" % (i, len(rows)))

    page = os.path.join(args.output_dir, "index.html")
    write_page(page, items, args.cell_width, args.candidates, total)
    print("done -> %s（%d 帧）" % (page, len(items)))


if __name__ == "__main__":
    main()
