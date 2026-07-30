#!/usr/bin/env python3
"""入水检测机位分析——公共工具模块。

集中数据集路径、manifest 读取、关键帧窗口、模型注册、帧解码、
COCO-17 骨架常量与目标选人启发式，供 predict / review / select_frames 统一复用。

数据来源：广州二沙（swimming-gz）水下 0 号平面正上方的 Orbbec 机位，
RGB 1280×720 @30fps，每段约 25 秒，覆盖准备、蹬壁、飞行、入水、滑行。
帧号口径：解码序、从 0 开始，与 manifest.csv / res.json 一致。
"""
import csv
import json
import os
from dataclasses import dataclass

import cv2
import numpy as np

# 仓库根（.../swim_fbx_demo），用于派生统一输出根。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 外部数据集根：通过环境变量或各 CLI 参数注入，不写死进代码路径以外的假设。
DATASET = os.environ.get(
    "WATER_ENTRY_DATASET_ROOT",
    "/Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-up/swimming-gz-bad",
)
CLIP_DIR = os.path.join(DATASET, "bk_export_202607")
MANIFEST = os.path.join(DATASET, "bk_export_manifest.csv")

OUTPUT_ROOT = os.environ.get(
    "WATER_ENTRY_OUTPUT_ROOT",
    os.path.join(PROJECT_ROOT, "outputs", "water_entry"),
)

# 模型注册：现网特化版、随包微调版、通用 COCO 版。
# 通用版不随数据集提供，首次使用时由 ultralytics 下载到 outputs/water_entry/weights/，
# 不落在仓库根目录（ultralytics 默认写 cwd）。
WEIGHTS_DIR = os.path.join(OUTPUT_ROOT, "weights")
MODELS = {
    "swimup": os.path.join(DATASET, "yolo11n-pose-swimup_20250919.pt"),
    "swimup_bk": os.path.join(DATASET, "yolo11n-pose-swimup-bk.pt"),
    "coco": os.path.join(WEIGHTS_DIR, "yolo11n-pose.pt"),
}
DEFAULT_MODELS = ["swimup", "swimup_bk", "coco"]

# 标注要点：必须覆盖 jump_frame-5 ~ entry_frame+20；触水前后各 3 帧最关键。
DEFAULT_PRE = 5
DEFAULT_POST = 20
ENTRY_RADIUS = 3

# COCO-17 关键点
KP_NAMES = ["nose", "left_eye", "right_eye", "left_ear", "right_ear",
            "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
            "left_wrist", "right_wrist", "left_hip", "right_hip",
            "left_knee", "right_knee", "left_ankle", "right_ankle"]
L_SHO, R_SHO, L_HIP, R_HIP = 5, 6, 11, 12
TORSO_KPS = (L_SHO, R_SHO, L_HIP, R_HIP)
SKELETON = [(5, 7), (7, 9), (6, 8), (8, 10), (5, 6), (5, 11), (6, 12),
            (11, 12), (11, 13), (13, 15), (12, 14), (14, 16),
            (0, 1), (0, 2), (1, 3), (2, 4), (0, 5), (0, 6)]

KP_CONF = 0.5      # 关键点可用阈值（躯干可用性统计与入水判据都用它）


@dataclass
class Clip:
    """manifest 一行 + 视频路径 + res.json 里的仰泳段落信息。

    注意 `water_frame`（manifest 的线上入水帧）在 `backstroke_applied=False` 时
    不可信：该字段来自 water_line 掩膜扫描，运动员还在扶壁蜷缩时就会命中。
    本机位实测中 47/111 条片段的 `backstroke.entry_frame` 比它晚 3~36 帧，
    人工抽查（20260717-101123: manifest 88 vs bk 119）确认晚的那个才是真入水。
    因此评测与窗口一律以 `ref_entry_frame` 为准，`water_frame` 只作对照记录。
    """
    name: str
    jump_frame: int
    water_frame: int
    angle: float
    backstroke_applied: bool
    note: str
    bk_entry_frame: int = -1        # res.json metadata.backstroke.entry_frame
    bk_jump_frame: int = -1         # 同上 reference_jump_frame
    bk_apex_frame: int = -1
    bk_status: str = ""
    bk_reject: str = ""
    l2r: bool = False               # 游进方向，本场地实测为右→左（False）

    @property
    def video(self):
        return os.path.join(CLIP_DIR, self.name + ".mp4")

    @property
    def res_json(self):
        return os.path.join(CLIP_DIR, self.name + "_res.json")

    @property
    def ref_entry_frame(self):
        """评测基准入水帧：优先 res.json 的仰泳 entry_frame，缺失时退回 water_frame。"""
        return self.bk_entry_frame if self.bk_entry_frame > 0 else self.water_frame

    @property
    def ref_jump_frame(self):
        return self.bk_jump_frame if self.bk_jump_frame > 0 else self.jump_frame

    @property
    def entry_source(self):
        return "backstroke" if self.bk_entry_frame > 0 else "manifest_water_frame"

    def window(self, pre=DEFAULT_PRE, post=DEFAULT_POST):
        """返回必须覆盖的帧号列表（含首尾）：起跳前 pre 帧 ~ 入水后 post 帧。

        两个入水口径不一致时取并集，保证真入水帧一定落在窗口内。
        """
        lo = max(0, min(self.ref_jump_frame, self.jump_frame) - pre)
        hi = max(self.ref_entry_frame, self.water_frame) + post
        return list(range(lo, hi + 1))

    def left_to_right(self):
        """游进方向：True 为左→右。本场地实测为右→左。"""
        return self.l2r


def _read_res(clip):
    """把 res.json 的仰泳字段填进 Clip；文件缺失或损坏时保留默认值。"""
    try:
        with open(clip.res_json) as f:
            meta = json.load(f).get("metadata", {})
    except (OSError, ValueError):
        return clip
    bk = meta.get("backstroke", {})
    clip.bk_entry_frame = int(bk.get("entry_frame", -1))
    clip.bk_jump_frame = int(bk.get("reference_jump_frame", -1))
    clip.bk_apex_frame = int(bk.get("apex_frame", -1))
    clip.bk_status = str(bk.get("status", ""))
    clip.bk_reject = str(bk.get("apply_reject_reason", ""))
    clip.l2r = bool(meta.get("direction", {}).get("left_to_right", False))
    return clip


def load_manifest(path=MANIFEST, include_notes=None):
    """读取 manifest.csv 并合入各片段 res.json。include_notes 为 None 时返回全部。

    note 取值：'' 正常；'suspected_false_positive' 疑似误触发；
    'autolabel_selection_failed' 视频可用但自动选人被干扰。
    """
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    clips = [Clip(name=r["clip"], jump_frame=int(r["jump_frame"]),
                  water_frame=int(r["water_frame"]), angle=float(r["angle"]),
                  backstroke_applied=r["backstroke_applied"] == "True",
                  note=r["note"]) for r in rows]
    clips = [_read_res(c) for c in clips]
    if include_notes is not None:
        clips = [c for c in clips if c.note in include_notes]
    return clips


def read_frames(video_path, indices):
    """顺序解码取出指定帧号（升序），返回 {frame_index: BGR ndarray}。

    这些片段只有 750 帧，顺序 grab 到目标位比 CAP_PROP_POS_FRAMES 随机定位更可靠。
    """
    want = sorted(set(int(i) for i in indices))
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise SystemExit("无法打开视频：%s" % video_path)
    out, cursor, pos = {}, 0, 0
    try:
        while cursor < len(want):
            target = want[cursor]
            while pos < target:
                if not cap.grab():
                    return out
                pos += 1
            ok, frame = cap.read()
            if not ok:
                return out
            out[target] = frame
            pos += 1
            cursor += 1
    finally:
        cap.release()
    return out


def torso_ok(kps_conf):
    """双肩 + 双胯四点是否都达到 KP_CONF（业务算入水角的最低要求）。"""
    return all(float(kps_conf[i]) >= KP_CONF for i in TORSO_KPS)


def midpoint(kps_xy, kps_conf, a, b):
    """两点中点；任一点低于 KP_CONF 返回 None。"""
    if float(kps_conf[a]) < KP_CONF or float(kps_conf[b]) < KP_CONF:
        return None
    return ((float(kps_xy[a][0]) + float(kps_xy[b][0])) / 2.0,
            (float(kps_xy[a][1]) + float(kps_xy[b][1])) / 2.0)


def estimate_entry_frame(per_frame, after_frame=None):
    """按已验证判据估计头部入水帧：肩中点与胯中点的上下关系翻转的那一帧。

    per_frame: [{frame, kps_xy, kps_conf, ...}]（已选定目标人物，缺检的项 kps_xy=None）
    after_frame: 只在该帧之后找翻转（传起跳帧，避开起跳前扶壁蜷缩造成的伪翻转）。
    返回 (entry_frame or None, [(frame, shoulder_y - hip_y or None)])。
    图像坐标 y 向下，肩在胯之上 => shoulder_y - hip_y < 0；入水后头下沉翻转为 > 0。
    """
    signs = []
    for rec in per_frame:
        if rec.get("kps_xy") is None:
            signs.append((rec["frame"], None))
            continue
        sho = midpoint(rec["kps_xy"], rec["kps_conf"], L_SHO, R_SHO)
        hip = midpoint(rec["kps_xy"], rec["kps_conf"], L_HIP, R_HIP)
        signs.append((rec["frame"], None if (sho is None or hip is None)
                      else sho[1] - hip[1]))
    valid = [(f, d) for f, d in signs if d is not None
             and (after_frame is None or f >= after_frame)]
    entry = None
    for (_f_prev, d_prev), (f_cur, d_cur) in zip(valid, valid[1:]):
        if d_prev < 0 <= d_cur:
            entry = f_cur
            break
    return entry, signs


def _centre(box):
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def link_tracks(detections, frame_width, max_gap=6, dist_ratio=0.15):
    """把逐帧检测框贪心串成轨迹（中心距最近、允许 max_gap 帧断裂）。

    max_gap 默认 6：现网模型在飞行段会连续丢几帧，跨过缺口继续接同一人，
    否则运动员轨迹会被切碎、位移判据失效。

    匹配半径是固定的 `dist_ratio × 画宽`，**不按缺口帧数放大**。放大过的版本
    实测更差：swimup 的 12 条片段空中段检出下降、其中 10 条直接归零，因为放宽的
    半径让轨迹跨缺口时接到画面里静止的无关目标（20260713-103240 上一条本该跟住
    运动员 46 帧的轨迹，变成锁死在 cx≈1028 的 3 帧静态目标）。宁可把轨迹切碎，
    也不要接错人——切碎只是少几帧，接错人会让整段指标失真。

    detections: [{frame, boxes: [[x1,y1,x2,y2],...]}]，按帧升序。
    返回 [[(frame, det_index), ...], ...]。
    """
    thresh = frame_width * dist_ratio
    tracks, open_tracks = [], []          # open_tracks: [(track, last_frame, last_centre)]
    for rec in detections:
        frame, boxes = rec["frame"], rec["boxes"]
        used = set()
        still_open = []
        for track, last_frame, last_c in open_tracks:
            if frame - last_frame > max_gap:
                tracks.append(track)
                continue
            best, best_d = None, thresh
            for i, box in enumerate(boxes):
                if i in used:
                    continue
                c = _centre(box)
                d = float(np.hypot(c[0] - last_c[0], c[1] - last_c[1]))
                if d < best_d:
                    best, best_d = i, d
            if best is None:
                still_open.append((track, last_frame, last_c))
            else:
                used.add(best)
                track.append((frame, best))
                still_open.append((track, frame, _centre(boxes[best])))
        for i, box in enumerate(boxes):
            if i not in used:
                still_open.append(([(frame, i)], frame, _centre(box)))
        open_tracks = still_open
    tracks.extend(track for track, _f, _c in open_tracks)
    return tracks


def pick_athlete_track(tracks, detections, left_to_right, min_len=3):
    """按「起跳后沿游进方向的净位移最大」选出运动员轨迹。

    README 的血泪经验：只看置信度会在入水瞬间被池边站立者抢走；只靠位移也会被
    前排泳道游进的人干扰。这里用位移方向 + 轨迹长度做第一版，泳道约束留待
    ROI 接入后再加。实测选错只有 2 例，且都是窗口内本就没有出发动作的片段。
    返回被选中的轨迹（可能为 None）。
    """
    best, best_score = None, None
    for track in tracks:
        if len(track) < min_len:
            continue
        f0, i0 = track[0]
        f1, i1 = track[-1]
        x0 = _centre(detections_box(detections, f0, i0))[0]
        x1 = _centre(detections_box(detections, f1, i1))[0]
        disp = (x1 - x0) if left_to_right else (x0 - x1)
        score = (disp, len(track))
        if best_score is None or score > best_score:
            best, best_score = track, score
    return best


def detections_box(detections, frame, index):
    for rec in detections:
        if rec["frame"] == frame:
            return rec["boxes"][index]
    raise KeyError((frame, index))


def resolve_device(name=None):
    """选择推理设备：显式指定优先，其次 MPS，最后 CPU。"""
    if name:
        return name
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def lazy_img_js(margin_px):
    """复核页共用的图片懒加载脚本：千级裁剪图一次性加载会卡死浏览器。

    review 与 annotate_preview 的页面布局各不相同（多模型横排 vs 候选帧序列），
    共用的只有「一堆 <img data-src> 进视口再取图」，预加载距离各自给。
    """
    return ("\nconst io=new IntersectionObserver((es)=>{for(const e of es){"
            "if(e.isIntersecting){\nconst i=e.target;if(i.dataset.src){"
            "i.src=i.dataset.src;delete i.dataset.src;}io.unobserve(i);}}},\n"
            "{rootMargin:'%dpx'});\n"
            "document.querySelectorAll('img[data-src]').forEach(i=>io.observe(i));\n"
            % margin_px)

