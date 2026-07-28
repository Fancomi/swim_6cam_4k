#!/usr/bin/env python3
"""对入水检测机位片段跑 YOLO-pose 预测，横向对比多个模型的检出率与入水帧估计。

对每个片段只推理 `jump_frame-PRE` ~ `entry_frame+POST` 的窗口帧（README §3.2），
统计四项指标：
  1. det_rate      —— 窗口内检出目标人物的帧占比；
  2. flight_rate   —— 起跳帧~入水帧（空中反弓段）的检出占比，现网模型的失明就在这里；
  3. entry_rate    —— 入水帧 ±3 帧的检出占比，业务量入水角的时刻；
  4. pred_entry    —— 用「肩中点/胯中点上下关系翻转」判据估计的头部入水帧，
                      与基准入水帧比较得到 entry_delta。

基准入水帧取 `res.json` 的 `metadata.backstroke.entry_frame`，不是 manifest 的
`water_frame`——后者在 `backstroke_applied=False` 的 47 条片段上偏早 3~36 帧
（见 common.Clip 的说明）。两者都作为列写进 metrics.csv 以便对照。

产物：outputs/water_entry/predict/<model>/metrics.csv 与 per_frame/<clip>.json。
"""
import argparse
import csv
import json
import os
import time

import numpy as np

from python.water_entry import common as C

METRIC_COLS = ["clip", "model", "note", "backstroke_applied", "entry_source",
               "jump_frame", "entry_frame", "manifest_water_frame",
               "manifest_angle", "window_frames", "det_frames", "det_rate",
               "flight_frames", "flight_det_frames", "flight_rate",
               "entry_window_frames", "entry_det_frames", "entry_rate",
               "torso_frames", "torso_rate",
               "pred_entry_frame", "entry_delta", "fallback", "seconds"]


def _run_batches(model, images, ordered, conf, imgsz, device, batch):
    """分批推理，返回与 ordered 对齐的 Results 列表。

    整段 60 帧一次喂给 MPS 后端偶发返回全零检测（实测 swimup_bk 在
    20260717-101123 上出现一次，重跑即恢复），分批既压住这个抖动也限制显存峰值。
    """
    out = []
    for i in range(0, len(ordered), batch):
        chunk = ordered[i:i + batch]
        out.extend(model.predict([images[f] for f in chunk], conf=conf,
                                 imgsz=imgsz, device=device, verbose=False))
    return out


def predict_clip(model, clip, pre, post, conf, imgsz, device, batch=16,
                 verify_empty=True):
    """对一个片段的窗口帧推理，选出运动员轨迹，返回逐帧记录与耗时。

    verify_empty：窗口内一帧都没检出时，用 CPU 复算一遍再定论——GPU 后端的偶发
    全零不能被当成模型失明写进指标。复算后仍为空才认为是真的检不出。
    """
    frames = clip.window(pre, post)
    images = C.read_frames(clip.video, frames)
    ordered = [f for f in frames if f in images]

    t0 = time.time()
    results = _run_batches(model, images, ordered, conf, imgsz, device, batch)
    fallback = ""
    if (verify_empty and device != "cpu"
            and not any(r.boxes is not None and len(r.boxes) for r in results)):
        results = _run_batches(model, images, ordered, conf, imgsz, "cpu", batch)
        fallback = "cpu_recheck"
    seconds = time.time() - t0

    detections = []
    for frame, res in zip(ordered, results):
        boxes = ([] if res.boxes is None or len(res.boxes) == 0
                 else res.boxes.xyxy.cpu().numpy().tolist())
        detections.append({"frame": frame, "boxes": boxes, "res": res})

    width = images[ordered[0]].shape[1] if ordered else 1280
    plain = [{"frame": d["frame"], "boxes": d["boxes"]} for d in detections]
    tracks = C.link_tracks(plain, width)
    track = C.pick_athlete_track(tracks, plain, clip.left_to_right())
    picked = dict(track or [])

    per_frame = []
    for det in detections:
        frame = det["frame"]
        rec = {"frame": frame, "n_det": len(det["boxes"]),
               "box": None, "conf": None, "kps_xy": None, "kps_conf": None}
        idx = picked.get(frame)
        if idx is not None:
            res = det["res"]
            rec["box"] = [round(v, 2) for v in det["boxes"][idx]]
            rec["conf"] = round(float(res.boxes.conf[idx]), 4)
            kp = res.keypoints
            rec["kps_xy"] = np.round(kp.xy[idx].cpu().numpy(), 2).tolist()
            rec["kps_conf"] = np.round(
                (kp.conf[idx].cpu().numpy() if kp.conf is not None
                 else np.ones(len(C.KP_NAMES))), 4).tolist()
        per_frame.append(rec)
    return per_frame, seconds, fallback


def summarize(clip, per_frame, seconds, model_name, fallback=""):
    det = [r for r in per_frame if r["kps_xy"] is not None]
    torso = [r for r in det if C.torso_ok(r["kps_conf"])]
    entry_ref = clip.ref_entry_frame
    jump = clip.ref_jump_frame

    flight = [r for r in per_frame if jump <= r["frame"] <= entry_ref]
    flight_det = [r for r in flight if r["kps_xy"] is not None]
    lo, hi = entry_ref - C.ENTRY_RADIUS, entry_ref + C.ENTRY_RADIUS
    near = [r for r in per_frame if lo <= r["frame"] <= hi]
    near_det = [r for r in near if r["kps_xy"] is not None]

    pred, _signs = C.estimate_entry_frame(per_frame, after_frame=jump)
    n = len(per_frame) or 1
    return {
        "clip": clip.name, "model": model_name, "note": clip.note,
        "backstroke_applied": int(clip.backstroke_applied),
        "entry_source": clip.entry_source,
        "jump_frame": jump, "entry_frame": entry_ref,
        "manifest_water_frame": clip.water_frame,
        "manifest_angle": clip.angle,
        "window_frames": len(per_frame), "det_frames": len(det),
        "det_rate": round(len(det) / n, 4),
        "flight_frames": len(flight), "flight_det_frames": len(flight_det),
        "flight_rate": round(len(flight_det) / (len(flight) or 1), 4),
        "entry_window_frames": len(near), "entry_det_frames": len(near_det),
        "entry_rate": round(len(near_det) / (len(near) or 1), 4),
        "torso_frames": len(torso), "torso_rate": round(len(torso) / n, 4),
        "pred_entry_frame": "" if pred is None else pred,
        "entry_delta": "" if pred is None else pred - entry_ref,
        "fallback": fallback,
        "seconds": round(seconds, 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=C.DEFAULT_MODELS,
                    help="模型键名（%s）或 .pt 路径，默认全部对比"
                         % "/".join(C.MODELS))
    ap.add_argument("--clips", nargs="*", default=None, help="只跑指定片段名")
    ap.add_argument("--limit", type=int, default=0, help="最多跑前 N 个片段（0 = 全部）")
    ap.add_argument("--skip-false-positive", action="store_true",
                    help="跳过 note=suspected_false_positive 的疑似误触发片段")
    ap.add_argument("--pre", type=int, default=C.DEFAULT_PRE,
                    help="窗口起点 = jump_frame - PRE（默认 %(default)s）")
    ap.add_argument("--post", type=int, default=C.DEFAULT_POST,
                    help="窗口终点 = entry_frame + POST（默认 %(default)s）")
    ap.add_argument("--conf", type=float, default=0.25, help="检测置信度阈值")
    ap.add_argument("--imgsz", type=int, default=800,
                    help="推理边长，训练配方为 800（默认 %(default)s）")
    ap.add_argument("--device", default=None, help="cpu / mps；默认自动选择")
    ap.add_argument("--batch", type=int, default=16,
                    help="每次 predict 的帧数（默认 %(default)s）")
    ap.add_argument("--no-verify-empty", action="store_true",
                    help="窗口全空时不做 CPU 复算（默认复算，用于排除 GPU 偶发全零）")
    ap.add_argument("--output-dir", default=os.path.join(C.OUTPUT_ROOT, "predict"))
    args = ap.parse_args()

    if not os.path.isdir(C.CLIP_DIR):
        raise SystemExit("缺少片段目录：%s（可用 WATER_ENTRY_DATASET_ROOT 覆盖）"
                         % C.CLIP_DIR)

    notes = {"", "autolabel_selection_failed"} if args.skip_false_positive else None
    clips = C.load_manifest(include_notes=notes)
    if args.clips:
        wanted = set(args.clips)
        clips = [c for c in clips if c.name in wanted]
    if args.limit:
        clips = clips[:args.limit]
    if not clips:
        raise SystemExit("没有匹配的片段")

    from ultralytics import YOLO
    device = C.resolve_device(args.device)
    print("device=%s  clips=%d  models=%s" % (device, len(clips), ",".join(args.models)))

    for key in args.models:
        weights = C.MODELS.get(key, key)
        name = key if key in C.MODELS else os.path.splitext(os.path.basename(key))[0]
        if key in C.MODELS and key != "coco" and not os.path.exists(weights):
            print("跳过 %s：权重不存在 %s" % (name, weights))
            continue
        if key == "coco" and not os.path.exists(weights):
            os.makedirs(os.path.dirname(weights), exist_ok=True)
            YOLO("yolo11n-pose.pt")             # 触发下载到 cwd
            os.replace("yolo11n-pose.pt", weights)
        model = YOLO(weights)
        out_dir = os.path.join(args.output_dir, name)
        frame_dir = os.path.join(out_dir, "per_frame")
        os.makedirs(frame_dir, exist_ok=True)

        rows = []
        for i, clip in enumerate(clips, 1):
            per_frame, seconds, fallback = predict_clip(
                model, clip, args.pre, args.post, args.conf, args.imgsz,
                device, args.batch, not args.no_verify_empty)
            row = summarize(clip, per_frame, seconds, name, fallback)
            rows.append(row)
            with open(os.path.join(frame_dir, clip.name + ".json"), "w") as f:
                json.dump({"clip": clip.name, "model": name,
                           "weights": weights, "conf": args.conf,
                           "imgsz": args.imgsz, "device": device,
                           "jump_frame": clip.ref_jump_frame,
                           "entry_frame": clip.ref_entry_frame,
                           "entry_source": clip.entry_source,
                           "manifest_water_frame": clip.water_frame,
                           "left_to_right": clip.left_to_right(),
                           "metrics": row, "frames": per_frame}, f)
            print("  [%s %3d/%d] %s det=%.2f flight=%.2f entry=%.2f "
                  "pred=%s(Δ%s) %.1fs%s"
                  % (name, i, len(clips), clip.name, row["det_rate"],
                     row["flight_rate"], row["entry_rate"],
                     row["pred_entry_frame"] or "-",
                     row["entry_delta"] if row["entry_delta"] != "" else "-",
                     row["seconds"], " [%s]" % fallback if fallback else ""))

        csv_path = os.path.join(out_dir, "metrics.csv")
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=METRIC_COLS)
            w.writeheader()
            w.writerows(rows)
        deltas = [r["entry_delta"] for r in rows if r["entry_delta"] != ""]
        print("%-12s det=%.3f flight=%.3f entry=%.3f torso=%.3f  "
              "pred_entry %d/%d, |Δ|<=2: %d  ->  %s"
              % (name,
                 float(np.mean([r["det_rate"] for r in rows])),
                 float(np.mean([r["flight_rate"] for r in rows])),
                 float(np.mean([r["entry_rate"] for r in rows])),
                 float(np.mean([r["torso_rate"] for r in rows])),
                 len(deltas), len(rows),
                 sum(1 for d in deltas if abs(d) <= 2), csv_path))


if __name__ == "__main__":
    main()
