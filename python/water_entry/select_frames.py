#!/usr/bin/env python3
"""从两个 swimup 模型的预测结果里挑出「模型做得差」的帧，作为增量标注候选。

只看 swimup 与 swimup_bk：待标注数据是给这两个模型做增量训练的，通用 COCO 模型的
结果不参与判断（它的失效模式与我们的训练集无关）。

七类信号（每帧可命中多个，`reasons` 列记录全部命中项）：

| 信号 | 含义 | 为什么值得标 |
| --- | --- | --- |
| `both_blind`   | 两个模型都 0 检出 | 训练集完全没覆盖的姿态，价值最高 |
| `both_reject`  | 有检出但选人都没接上 | 目标存在却跟丢，标了能同时修检测与跟踪 |
| `one_miss`     | 一个模型检出、另一个没有 | 一个模型已能检出 => 不是不可见，是另一个模型的缺口 |
| `diff_person`  | 两框 IoU 低 | 两模型指向不同的人，标注可消除歧义 |
| `kp_disagree`  | 同一人但关键点分歧大 | 关键点精度不足，正是需要人工精修的地方 |
| `torso_broken` | 有框但躯干四点不全 | 业务算入水角的最低要求都没满足 |
| `sign_flip`    | 两模型对 sho-hip 符号判断相反 | 直接影响入水帧判定的那一帧 |

选出的帧按 `score` 排序，并做时序去重（同片段内相邻帧只留代表帧），避免把
连续 5 帧几乎相同的画面都送去标注。
"""
import argparse
import csv
import json
import os
from collections import defaultdict

import numpy as np

from python.water_entry import common as C

MODEL_A = "swimup"
MODEL_B = "swimup_bk"

CANDIDATE_COLS = ["clip", "frame", "offset_to_entry", "phase", "note",
                  "entry_source", "reasons", "score",
                  "a_conf", "b_conf", "a_torso", "b_torso",
                  "iou", "kp_mean_norm", "kp_max_norm",
                  "a_sign", "b_sign", "n_det_a", "n_det_b"]


def _iou(box_a, box_b):
    """两框 IoU；任一为 None 返回 None。"""
    if box_a is None or box_b is None:
        return None
    x1 = max(box_a[0], box_b[0])
    y1 = max(box_a[1], box_b[1])
    x2 = min(box_a[2], box_b[2])
    y2 = min(box_a[3], box_b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, box_a[2] - box_a[0]) * max(0.0, box_a[3] - box_a[1])
    area_b = max(0.0, box_b[2] - box_b[0]) * max(0.0, box_b[3] - box_b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _diag(box):
    return float(np.hypot(box[2] - box[0], box[3] - box[1]))


def _torso_count(kps_conf):
    if kps_conf is None:
        return 0
    return sum(1 for i in C.TORSO_KPS if float(kps_conf[i]) >= C.KP_CONF)


def _sho_hip_sign(rec):
    """肩中点相对胯中点的上下符号：-1 肩在上（水面上），+1 肩在下（已入水）。"""
    if rec.get("kps_xy") is None:
        return None
    sho = C.midpoint(rec["kps_xy"], rec["kps_conf"], C.L_SHO, C.R_SHO)
    hip = C.midpoint(rec["kps_xy"], rec["kps_conf"], C.L_HIP, C.R_HIP)
    if sho is None or hip is None:
        return None
    return -1 if sho[1] < hip[1] else 1


def _kp_disagreement(rec_a, rec_b):
    """两模型共同可见关键点的像素距离，按框对角线归一。

    返回 (mean_px, max_px, mean_norm, max_norm, n_shared)；无共同可见点返回全 None。
    """
    xy_a, cf_a = rec_a["kps_xy"], rec_a["kps_conf"]
    xy_b, cf_b = rec_b["kps_xy"], rec_b["kps_conf"]
    dists = [float(np.hypot(xy_a[i][0] - xy_b[i][0], xy_a[i][1] - xy_b[i][1]))
             for i in range(len(C.KP_NAMES))
             if cf_a[i] >= C.KP_CONF and cf_b[i] >= C.KP_CONF]
    if not dists:
        return (None,) * 4 + (0,)
    scale = (_diag(rec_a["box"]) + _diag(rec_b["box"])) / 2.0 or 1.0
    mean_px, max_px = float(np.mean(dists)), float(np.max(dists))
    return mean_px, max_px, mean_px / scale, max_px / scale, len(dists)


def phase_of(frame, jump_frame, entry_frame, radius=C.ENTRY_RADIUS):
    """帧所处阶段：entry（入水前后 radius 帧）> flight（起跳到入水）> pre / post。"""
    if abs(frame - entry_frame) <= radius:
        return "entry"
    if jump_frame <= frame <= entry_frame:
        return "flight"
    return "pre" if frame < jump_frame else "post"


# 各信号的基础分。both_blind 最高：训练集完全没覆盖的姿态最值得补。
REASON_SCORE = {
    "both_blind": 100,
    "both_reject": 70,
    "one_miss": 60,
    "diff_person": 55,
    "sign_flip": 50,
    "kp_disagree": 30,
    "torso_broken": 25,
}
# 阶段权重：入水前后是业务量角的时刻，飞行段是已知短板，起跳前的扶壁段价值最低。
PHASE_WEIGHT = {"entry": 1.6, "flight": 1.25, "post": 1.0, "pre": 0.5}


def analyze_frame(rec_a, rec_b, frame, jump_frame, entry_frame, thresholds):
    """比较同一帧的两模型记录，返回 (reasons, 度量字典)。"""
    box_a, box_b = rec_a.get("box"), rec_b.get("box")
    iou = _iou(box_a, box_b)
    torso_a, torso_b = _torso_count(rec_a.get("kps_conf")), _torso_count(rec_b.get("kps_conf"))
    sign_a, sign_b = _sho_hip_sign(rec_a), _sho_hip_sign(rec_b)
    kp_mean_px = kp_max_px = kp_mean_norm = kp_max_norm = None

    reasons = []
    if box_a is None and box_b is None:
        if rec_a.get("n_det", 0) == 0 and rec_b.get("n_det", 0) == 0:
            reasons.append("both_blind")
        else:
            reasons.append("both_reject")
    elif box_a is None or box_b is None:
        reasons.append("one_miss")
    else:
        if iou < thresholds["iou"]:
            reasons.append("diff_person")
        else:
            kp_mean_px, kp_max_px, kp_mean_norm, kp_max_norm, shared = \
                _kp_disagreement(rec_a, rec_b)
            if shared and kp_mean_norm >= thresholds["kp_mean_norm"]:
                reasons.append("kp_disagree")
            if sign_a is not None and sign_b is not None and sign_a != sign_b:
                reasons.append("sign_flip")
        if min(torso_a, torso_b) < 4:
            reasons.append("torso_broken")

    metrics = {
        "iou": None if iou is None else round(iou, 4),
        "kp_mean_norm": None if kp_mean_norm is None else round(kp_mean_norm, 4),
        "kp_max_norm": None if kp_max_norm is None else round(kp_max_norm, 4),
        "kp_mean_px": None if kp_mean_px is None else round(kp_mean_px, 2),
        "kp_max_px": None if kp_max_px is None else round(kp_max_px, 2),
        "a_conf": rec_a.get("conf"), "b_conf": rec_b.get("conf"),
        "a_torso": torso_a, "b_torso": torso_b,
        "a_sign": sign_a, "b_sign": sign_b,
        "n_det_a": rec_a.get("n_det", 0), "n_det_b": rec_b.get("n_det", 0),
    }
    return reasons, metrics


def score_frame(reasons, phase, metrics):
    """信号基础分取最大值，叠加次要信号的一成加成，再乘阶段权重。

    取最大值而非求和，避免「一堆弱信号」压过「一个强信号」——both_blind 的一帧
    比三个弱信号叠加的一帧更值得标。
    """
    if not reasons:
        return 0.0
    base = max(REASON_SCORE[r] for r in reasons)
    extra = sum(REASON_SCORE[r] for r in reasons) - base
    return round((base + 0.1 * extra) * PHASE_WEIGHT[phase], 2)


def collect(predict_dir, notes, thresholds, clips=None, max_offset=None,
            min_offset=None, require_verified_entry=True):
    """遍历两个模型的 per_frame 结果，返回全部命中信号的候选帧。

    max_offset / min_offset 限制 `frame - entry_frame` 的范围。入水 6 帧之后
    运动员已没入水面，两模型开始各自锁住不同的水花伪影（实测 offset +6~+12 有
    16.6% 的帧两框 IoU<0.3，+13 之后升到 75%），那种分歧不是姿态质量问题，
    人工也标不出关键点。

    require_verified_entry：只保留 `entry_source == "backstroke"` 的片段。另外
    4 条片段的基准入水帧退化成 manifest 的 water_frame，抽帧确认过它偏早若干帧
    （20260707-105111 标 f93，实际 f98 之后才入水），偏移量与阶段权重都不可信。
    """
    manifest = {c.name: c for c in C.load_manifest()}
    dir_a = os.path.join(predict_dir, MODEL_A, "per_frame")
    dir_b = os.path.join(predict_dir, MODEL_B, "per_frame")
    for d in (dir_a, dir_b):
        if not os.path.isdir(d):
            raise SystemExit("缺少预测结果：%s（先运行 python -m "
                             "python.water_entry.predict）" % d)

    names = sorted(set(os.listdir(dir_a)) & set(os.listdir(dir_b)))
    names = [n[:-5] for n in names if n.endswith(".json")]
    if clips:
        names = [n for n in names if n in clips]

    rows, totals = [], {"clips": 0, "frames": 0, "frames_in_range": 0}
    for name in names:
        clip = manifest.get(name)
        note = clip.note if clip else ""
        if notes is not None and note not in notes:
            continue
        with open(os.path.join(dir_a, name + ".json")) as f:
            pay_a = json.load(f)
        with open(os.path.join(dir_b, name + ".json")) as f:
            pay_b = json.load(f)
        if require_verified_entry and pay_a["entry_source"] != "backstroke":
            continue
        by_a = {r["frame"]: r for r in pay_a["frames"]}
        by_b = {r["frame"]: r for r in pay_b["frames"]}
        shared = sorted(set(by_a) & set(by_b))
        jump, entry = pay_a["jump_frame"], pay_a["entry_frame"]

        totals["clips"] += 1
        totals["frames"] += len(shared)
        totals["frames_in_range"] += sum(
            1 for f in shared
            if (max_offset is None or f - entry <= max_offset)
            and (min_offset is None or f - entry >= min_offset))

        for frame in shared:
            offset = frame - entry
            if max_offset is not None and offset > max_offset:
                continue
            if min_offset is not None and offset < min_offset:
                continue
            reasons, metrics = analyze_frame(by_a[frame], by_b[frame], frame,
                                             jump, entry, thresholds)
            if not reasons:
                continue
            phase = phase_of(frame, jump, entry)
            rows.append(dict(metrics, clip=name, frame=frame,
                             offset_to_entry=offset, phase=phase,
                             note=note, entry_source=pay_a["entry_source"],
                             reasons="|".join(reasons),
                             score=score_frame(reasons, phase, metrics)))
    return rows, totals


def dedupe(rows, min_gap):
    """同片段内按分数贪心保留，抑制与已保留帧间隔小于 min_gap 的帧。

    连续几帧的画面几乎相同，全标是浪费标注预算；只留每段里分数最高的代表帧。
    """
    if min_gap <= 1:
        return list(rows)
    kept, per_clip = [], defaultdict(list)
    for row in sorted(rows, key=lambda r: (-r["score"], r["clip"], r["frame"])):
        if all(abs(row["frame"] - f) >= min_gap for f in per_clip[row["clip"]]):
            per_clip[row["clip"]].append(row["frame"])
            kept.append(row)
    return kept


def cap_per_clip(rows, limit):
    """每片段最多保留 limit 帧（按分数），避免少数片段吃掉全部标注预算。"""
    if limit <= 0:
        return list(rows)
    count, kept = defaultdict(int), []
    for row in sorted(rows, key=lambda r: (-r["score"], r["clip"], r["frame"])):
        if count[row["clip"]] < limit:
            count[row["clip"]] += 1
            kept.append(row)
    return kept


def summarize(rows, totals, label):
    """打印命中量、信号分布、阶段分布与每片段帧数分布。"""
    denom = totals["frames_in_range"] or 1
    print("\n=== %s ===" % label)
    print("候选帧 %d / 窗口内 %d 帧（%.1f%%；全窗口 %d 帧），覆盖片段 %d / %d"
          % (len(rows), totals["frames_in_range"], 100.0 * len(rows) / denom,
             totals["frames"], len({r["clip"] for r in rows}), totals["clips"]))
    by_reason = defaultdict(int)
    for row in rows:
        for reason in row["reasons"].split("|"):
            by_reason[reason] += 1
    print("信号命中（可重叠）：")
    for reason in sorted(by_reason, key=lambda r: -by_reason[r]):
        print("  %-13s %5d" % (reason, by_reason[reason]))
    by_phase, by_note = defaultdict(int), defaultdict(int)
    for row in rows:
        by_phase[row["phase"]] += 1
        by_note[row["note"] or "(clean)"] += 1
    print("阶段：" + "  ".join("%s=%d" % (p, by_phase[p])
                             for p in ("entry", "flight", "post", "pre")
                             if by_phase[p]))
    print("note：" + "  ".join("%s=%d" % (n, by_note[n]) for n in sorted(by_note)))
    per_clip = defaultdict(int)
    for row in rows:
        per_clip[row["clip"]] += 1
    if per_clip:
        counts = np.array(sorted(per_clip.values()))
        print("每片段帧数：min=%d median=%d p90=%d max=%d"
              % (counts.min(), int(np.median(counts)),
                 int(np.percentile(counts, 90)), counts.max()))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--predict-dir", default=os.path.join(C.OUTPUT_ROOT, "predict"))
    ap.add_argument("--clips", nargs="*", default=None, help="只看指定片段")
    ap.add_argument("--include-false-positive", action="store_true",
                    help="把 note=suspected_false_positive 的片段也纳入（默认排除，"
                         "它们多半没有真实出发动作，标了对增量训练没帮助）")
    ap.add_argument("--iou", type=float, default=0.4,
                    help="低于该 IoU 视为两模型指向不同的人（默认 %(default)s）")
    ap.add_argument("--kp-mean-norm", type=float, default=0.10,
                    help="关键点平均分歧 / 框对角线，超过即判分歧大（默认 %(default)s）")
    ap.add_argument("--min-gap", type=int, default=3,
                    help="同片段内相邻候选帧的最小间隔（默认 %(default)s，1 = 不去重）")
    ap.add_argument("--max-offset", type=int, default=6,
                    help="只取 frame - entry_frame <= N 的帧（默认 %(default)s；"
                         "再往后运动员已没入水面，两模型各自锁住不同的水花伪影，"
                         "那种分歧人工也标不出来）")
    ap.add_argument("--min-offset", type=int, default=None,
                    help="只取 frame - entry_frame >= N 的帧（默认不限）")
    ap.add_argument("--allow-unverified-entry", action="store_true",
                    help="纳入基准入水帧退化为 manifest water_frame 的 4 条片段"
                         "（默认排除，那些片段的入水帧本身不可信）")
    ap.add_argument("--per-clip", type=int, default=0,
                    help="每片段最多保留几帧（默认 0 = 不限）")
    ap.add_argument("--top", type=int, default=0,
                    help="全局只保留分数最高的 N 帧（默认 0 = 不限）")
    ap.add_argument("--output", default=os.path.join(C.OUTPUT_ROOT,
                                                    "annotate_candidates.csv"))
    args = ap.parse_args()

    notes = None if args.include_false_positive else {"", "autolabel_selection_failed"}
    thresholds = {"iou": args.iou, "kp_mean_norm": args.kp_mean_norm}
    raw, totals = collect(args.predict_dir, notes, thresholds,
                          set(args.clips) if args.clips else None,
                          args.max_offset, args.min_offset,
                          not args.allow_unverified_entry)
    summarize(raw, totals, "全部命中信号的帧（未去重）")

    rows = dedupe(raw, args.min_gap)
    summarize(rows, totals, "时序去重后（min_gap=%d）" % args.min_gap)

    if args.per_clip:
        rows = cap_per_clip(rows, args.per_clip)
        summarize(rows, totals, "每片段上限 %d 帧后" % args.per_clip)
    if args.top:
        rows = sorted(rows, key=lambda r: (-r["score"], r["clip"], r["frame"]))[:args.top]
        summarize(rows, totals, "取分数最高 %d 帧后" % args.top)

    rows.sort(key=lambda r: (-r["score"], r["clip"], r["frame"]))
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CANDIDATE_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("\ndone -> %s（%d 帧）" % (args.output, len(rows)))


if __name__ == "__main__":
    main()
