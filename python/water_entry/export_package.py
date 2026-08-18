#!/usr/bin/env python3
"""把候选帧导出成可直接交付标注的数据包（图片 + 清单 + 预标注 + 说明）。

导出的是**无叠加的原始帧**——质检页那套骨架叠加只用于我们自己判断该不该标，
真的送去标注时叠加线条会干扰标注员判断。模型预测另以 COCO 关键点格式随包给出，
标注员在此基础上精修，不必从零点起（数据集交付说明 §4 用的就是这个流程）。

预标注默认取 `swimup_bk`（实测空中段与入水段检出率最高）；该帧若 bk 缺检则退回
`swimup`，两者都缺检时只给图不给预标注，并在清单里标出来。

产物（--output-dir，默认 outputs/pose/annotate_package/）：
  images/<clip>_f<frame>.jpg    原始帧，1280×720 无叠加
  manifest.csv                  逐帧的信号、分数、阶段、偏移与预标注来源
  prelabel_coco.json            COCO keypoints 格式的模型预标注
  README.txt                    交付说明：口径、标注要点、字段含义
"""
import argparse
import json
import os
import zipfile
from collections import defaultdict

import cv2

from python.common.media import read_frames, write_image
from python.common.tables import read_rows, write_rows
from python.water_entry import common as C
from python.water_entry.select_frames import MODEL_A, MODEL_B

MANIFEST_COLS = ["image", "clip", "frame", "offset_to_entry", "phase",
                 "jump_frame", "entry_frame", "note", "reasons", "score",
                 "prelabel_source", "prelabel_conf"]

# 预标注优先级：bk 检出率最高，缺检时退回现网模型。
PRELABEL_ORDER = (MODEL_B, MODEL_A)


def load_payloads(predict_dir, clips):
    """读取两个模型的 per_frame 结果，返回 {clip: {model: {frame: rec}}}。"""
    out = {}
    for clip in clips:
        per_model = {}
        for model in PRELABEL_ORDER:
            path = os.path.join(predict_dir, model, "per_frame", clip + ".json")
            with open(path) as f:
                payload = json.load(f)
            per_model[model] = {r["frame"]: r for r in payload["frames"]}
        out[clip] = per_model
    return out


def pick_prelabel(per_model, frame):
    """返回 (model_name, rec) —— 第一个在该帧有框的模型；都没有则 (None, None)。"""
    for model in PRELABEL_ORDER:
        rec = per_model.get(model, {}).get(frame)
        if rec and rec.get("box") is not None:
            return model, rec
    return None, None


def coco_annotation(ann_id, image_id, rec):
    """把一帧的框与 17 点转成 COCO keypoints 标注。

    COCO 的 v 位：2 = 已标注且可见，0 = 未标注。低于 KP_CONF 的点记为 0，
    让标注工具把它显示为「待补」而不是一个错误的既有点。
    """
    x1, y1, x2, y2 = rec["box"]
    kps, visible = [], 0
    for (px, py), conf in zip(rec["kps_xy"], rec["kps_conf"]):
        if conf >= C.KP_CONF:
            kps.extend([round(px, 1), round(py, 1), 2])
            visible += 1
        else:
            kps.extend([0, 0, 0])
    return {
        "id": ann_id, "image_id": image_id, "category_id": 1,
        "bbox": [round(x1, 1), round(y1, 1),
                 round(x2 - x1, 1), round(y2 - y1, 1)],
        "area": round((x2 - x1) * (y2 - y1), 1),
        "iscrowd": 0, "num_keypoints": visible, "keypoints": kps,
        "score": rec["conf"],
    }


COCO_CATEGORY = {
    "id": 1, "name": "person", "supercategory": "person",
    "keypoints": C.KP_NAMES,
    "skeleton": [[a + 1, b + 1] for a, b in C.SKELETON],
}

README_TEXT = """\
入水检测机位 —— 增量标注数据包
================================

来源：{dataset}
生成：{count} 帧，取自 {clips} 条片段
筛选：{source_csv}

一、这批帧是怎么选出来的
------------------------
这些不是随机抽帧，而是两个在训练中的模型（yolo11n-pose-swimup 与其微调版
swimup-bk）**给出不一致结果**的帧。分歧位置就是模型的薄弱处，标注这些帧对
提升模型的收益远高于标注它们已经做对的帧。

manifest.csv 的 reasons 列记录该帧命中了哪种分歧：

  kp_disagree    两模型框住同一个人，但关键点位置差异大（本包的主要类型）
  sign_flip      两模型对「肩中点在胯中点上方还是下方」判断相反
  one_miss       只有一个模型检出了人
  both_blind     两个模型都没检出
  diff_person    两模型框住了不同的人
  torso_broken   有框但双肩双胯四点不全
  both_reject    有检出但都没被选为目标

二、标注要点
------------
1. 【本包绝大多数帧要做的是精修关键点，不是重画框】
   多数帧两模型的框和选人都是对的，差的只是关节点精度。请在 prelabel 的基础上
   把点拖到正确位置，而不是从零开始。

2. 【选对人】
   画面里常同时存在：出发运动员、前排泳道正在游进的人、岸上或跳台旁的教练。
   本场地游进方向为 **右 → 左**。运动员是「起跳后沿该方向大幅位移、且位于出发台
   所在泳道」的那个目标。入水瞬间运动员置信度会骤降，容易被池边站立者抢走。

3. 【最关键的帧段】
   manifest.csv 的 phase 列标出了该帧位置：
     entry   入水前后 3 帧 —— 业务上要测入水角的时刻，精度要求最高
     flight  起跳到入水的空中反弓段 —— 现网模型的已知短板
     pre     起跳前（运动员扶壁蜷缩，身体近乎竖直）
     post    入水后

4. 【仰泳准备动作在水中】
   起跳前运动员扶壁蜷缩、身体近乎竖直。这些帧对姿态识别有用，照常标注；
   但业务侧计算入水角时会排除，两者不要混为一谈。

5. 【入水判据】
   肩中点与胯中点的上下关系翻转的那一帧 = 头部入水帧。命中 sign_flip 的帧正是
   两模型在这条判据上冲突的地方，标注时请特别确认肩、胯四个点的位置。

三、文件说明
------------
images/               无叠加原始帧，1280x720，文件名 <片段>_f<帧号>.jpg
                      帧号为解码序、从 0 开始，与片段的 res.json 口径一致
manifest.csv          逐帧信息，列含义见下
prelabel_coco.json    模型预标注，COCO keypoints 格式（17 点，v=0 表示该点
                      模型置信度不足、需要人工补齐）

manifest.csv 列：
  image             对应 images/ 下的文件名
  clip, frame       片段名与帧号
  offset_to_entry   该帧相对入水帧的偏移（负数为入水前）
  phase             见「标注要点 3」
  jump_frame        该片段的起跳帧
  entry_frame       该片段的入水帧（基准口径，见下）
  note              片段级质量标记，空为正常；autolabel_selection_failed 表示
                    视频可用但自动选人曾被干扰，标注时尤其注意选对目标
  reasons           命中的分歧信号，多个以 | 分隔
  score             筛选优先级分数，越高越值得优先标
  prelabel_source   预标注来自哪个模型；none 表示两模型都没检出，需从零标注
  prelabel_conf     该预标注的检测置信度

四、口径约定
------------
- 帧号：解码序、从 0 开始。
- 入水帧（entry_frame）取自各片段 res.json 的 metadata.backstroke.entry_frame，
  **不是** manifest.csv 的 water_frame——后者在部分片段上偏早 3~36 帧。
- 关键点为 COCO-17，顺序见 prelabel_coco.json 的 categories[0].keypoints。
"""


def write_readme(path, dataset, count, clips, source_csv):
    with open(path, "w") as f:
        f.write(README_TEXT.format(dataset=dataset, count=count, clips=clips,
                                  source_csv=os.path.basename(source_csv)))


def export(rows, predict_dir, out_dir, quality):
    """导出图片、manifest 与 COCO 预标注，返回 (manifest 行, 统计字典)。

    按片段分组解码：同片段的多帧一次顺序解码取完，避免逐帧重开视频。
    """
    images_dir = os.path.join(out_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    by_clip = defaultdict(list)
    for row in rows:
        by_clip[row["clip"]].append(row)
    payloads = load_payloads(predict_dir, sorted(by_clip))

    manifest, coco_images, coco_anns = [], [], []
    stats = defaultdict(int)
    image_id = ann_id = 0

    for clip in sorted(by_clip):
        wanted = sorted({int(r["frame"]) for r in by_clip[clip]})
        frames = read_frames(os.path.join(C.CLIP_DIR, clip + ".mp4"), wanted)
        for row in sorted(by_clip[clip], key=lambda r: int(r["frame"])):
            frame = int(row["frame"])
            if frame not in frames:
                stats["missing_frame"] += 1
                print("  跳过 %s f%d：视频里取不到该帧" % (clip, frame))
                continue
            image_id += 1
            filename = "%s_f%03d.jpg" % (clip, frame)
            image = frames[frame]
            write_image(os.path.join(images_dir, filename), image,
                        "package frame")

            model, rec = pick_prelabel(payloads[clip], frame)
            stats["prelabel_" + (model or "none")] += 1
            coco_images.append({"id": image_id, "file_name": filename,
                                "width": image.shape[1], "height": image.shape[0],
                                "clip": clip, "frame": frame})
            if rec is not None:
                ann_id += 1
                coco_anns.append(coco_annotation(ann_id, image_id, rec))

            manifest.append({
                "image": filename, "clip": clip, "frame": frame,
                "offset_to_entry": row["offset_to_entry"],
                "phase": row["phase"],
                "jump_frame": "", "entry_frame": "",
                "note": row["note"], "reasons": row["reasons"],
                "score": row["score"],
                "prelabel_source": model or "none",
                "prelabel_conf": "" if rec is None else rec["conf"],
            })

    # jump/entry 是片段级信息，从 per_frame payload 的头部补齐。
    heads = {}
    for clip in sorted(by_clip):
        with open(os.path.join(predict_dir, MODEL_B, "per_frame",
                              clip + ".json")) as f:
            head = json.load(f)
        heads[clip] = (head["jump_frame"], head["entry_frame"])
    for row in manifest:
        row["jump_frame"], row["entry_frame"] = heads[row["clip"]]

    write_rows(os.path.join(out_dir, "manifest.csv"), MANIFEST_COLS, manifest)
    with open(os.path.join(out_dir, "prelabel_coco.json"), "w") as f:
        json.dump({"info": {"description": "water-entry incremental annotation "
                                           "candidates (model prelabels)"},
                   "images": coco_images, "annotations": coco_anns,
                   "categories": [COCO_CATEGORY]}, f)
    return manifest, stats


def make_zip(out_dir, zip_path):
    """把导出目录打成 zip，返回 (文件数, 字节数)。"""
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _dirs, files in os.walk(out_dir):
            for name in sorted(files):
                full = os.path.join(root, name)
                z.write(full, os.path.relpath(full, os.path.dirname(out_dir)))
                count += 1
    return count, os.path.getsize(zip_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--candidates",
                    default=os.path.join(C.POSE_ROOT, "annotate_candidates.csv"),
                    help="select_frames 输出的 CSV（默认 %(default)s）")
    ap.add_argument("--predict-dir", default=os.path.join(C.POSE_ROOT, "predict"))
    ap.add_argument("--limit", type=int, default=0,
                    help="只导出前 N 帧（CSV 已按分数降序，默认 0 = 全部）")
    ap.add_argument("--quality", type=int, default=95,
                    help="JPEG 质量，标注用不宜过低（默认 %(default)s）")
    ap.add_argument("--output-dir",
                    default=os.path.join(C.POSE_ROOT, "annotate_package"))
    ap.add_argument("--zip", dest="zip_path", default=None,
                    help="打包路径（默认 <output-dir>.zip；--no-zip 可跳过）")
    ap.add_argument("--no-zip", action="store_true", help="只导出目录，不打包")
    args = ap.parse_args()

    if not os.path.exists(args.candidates):
        raise SystemExit("找不到候选 CSV：%s（先运行 python -m "
                         "python.water_entry.select_frames）" % args.candidates)
    rows = read_rows(args.candidates)
    if args.limit:
        rows = rows[:args.limit]
    if not rows:
        raise SystemExit("候选 CSV 是空的")

    os.makedirs(args.output_dir, exist_ok=True)
    print("导出 %d 帧 -> %s" % (len(rows), args.output_dir))
    manifest, stats = export(rows, args.predict_dir, args.output_dir, args.quality)
    clips = len({r["clip"] for r in manifest})
    write_readme(os.path.join(args.output_dir, "README.txt"),
                 C.DATASET, len(manifest), clips, args.candidates)

    print("图片 %d 张，覆盖片段 %d 条" % (len(manifest), clips))
    print("预标注来源：" + "  ".join(
        "%s=%d" % (k.replace("prelabel_", ""), v)
        for k, v in sorted(stats.items()) if k.startswith("prelabel_")))
    if stats.get("missing_frame"):
        print("解码取不到的帧：%d" % stats["missing_frame"])

    if not args.no_zip:
        zip_path = args.zip_path or (args.output_dir.rstrip("/") + ".zip")
        count, size = make_zip(args.output_dir, zip_path)
        print("done -> %s（%d 个文件，%.1f MB）"
              % (zip_path, count, size / 1024.0 / 1024.0))
    else:
        print("done -> %s（未打包）" % args.output_dir)


if __name__ == "__main__":
    main()
