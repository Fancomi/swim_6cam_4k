#!/usr/bin/env python3
"""游泳数据集分析——公共工具模块。

集中路径常量、相机帧枚举、中值帧/颜色差计算、标注工程读取、字体加载等，
供 detect / export / interpolate / render 等脚本统一复用。
"""
import glob
import json
import os
import re
import numpy as np
from PIL import Image, ImageFont

# 仓库根（.../swim_fbx_demo），用于派生所有生成产物的统一输出根。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 外部数据集根：通过环境变量或各 CLI 参数注入，不写死到某个项目内路径。
DATASET = os.environ.get(
    "ANNOTATION_PREVIEW_DATASET_ROOT",
    "/Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-xlj-under-grids",
)
SNAP_DIR = os.path.join(DATASET, "snapshots")
OBJ_DIR = os.path.join(DATASET, "object-frames")
PROJECT_JSON = os.path.join(OBJ_DIR, "dot_label_project.json")

# annotation preview 全部生成产物（CSV / grid / preview / 导出帧）的统一输出根，
# 默认落在仓库内被 .gitignore 忽略的 outputs/annotation_preview/，可用环境变量覆盖。
OUTPUT_ROOT = os.environ.get(
    "ANNOTATION_PREVIEW_OUTPUT_ROOT",
    os.path.join(PROJECT_ROOT, "outputs", "annotation_preview"),
)

DIST_THRESH = 40                                    # 判定像素变化的 RGB 欧氏距离阈值
N_ROWS = 9                                          # 每帧标注点数（一列 9 点）

# 相机沿全景一字排开：A16 左端 → A1 右端
CAMS_PANO = ["underA%d" % i for i in range(16, 0, -1)]
CAMS_ASC = ["underA%d" % i for i in range(1, 17)]


def load_font(size):
    """加载等宽粗体字体，失败回退默认。"""
    for p in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/System/Library/Fonts/Helvetica.ttc"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                pass
    return ImageFont.load_default()


def frame_index(path):
    """从导出文件名 f<NN>_... 解析全局帧号（1..50）。"""
    m = re.search(r"/?f(\d+)_", path)
    return int(m.group(1)) if m else -1


def frames_for_camera(cam):
    """返回某相机跨全部快照的 [(snapshot_id, path)]，按快照时间排序。"""
    out = []
    for d in sorted(glob.glob(os.path.join(SNAP_DIR, "raw_*"))):
        hits = glob.glob(os.path.join(d, "*__%s.jpg" % cam))
        if hits:
            out.append((os.path.basename(d), hits[0]))
    return out


def median_dist(paths):
    """读入一组同尺寸图像，返回 (中值帧 uint8(H,W,3), 到中值帧的逐像素距离 (N,H,W))。"""
    stack = np.stack(
        [np.asarray(Image.open(p).convert("RGB"), dtype=np.uint8) for p in paths], axis=0)
    median = np.median(stack, axis=0)
    dist = np.sqrt(((stack.astype(np.float32) - median) ** 2).sum(axis=3))
    return median.astype(np.uint8), dist


def load_project():
    """读取打点标注工程 json。"""
    with open(PROJECT_JSON) as f:
        return json.load(f)


def group_by_camera(doc):
    """把工程按相机分组：{cam: [(frame_index, relpath, points)]}，按帧号排序。"""
    groups = {}
    for im in doc["images"]:
        cam = im["image"].split("/")[0]
        groups.setdefault(cam, []).append(
            (frame_index(im["image"]), im["image"], im["points"]))
    for g in groups.values():
        g.sort()
    return groups


def find_image(relpath):
    """在 object-frames 下按相对路径通配定位实际图像文件。"""
    hits = glob.glob(os.path.join(OBJ_DIR, relpath))
    return hits[0] if hits else None


def _diff_experiment(cams):
    """阈值实验 CLI：打印各相机每帧背景差分数，辅助人工选阈值。"""
    for cam in cams:
        fr = frames_for_camera(cam)
        _median, dist = median_dist([p for _, p in fr])
        print("\n==== %s  (%d frames) ====" % (cam, len(fr)))
        print("%-28s %8s %8s %8s" % ("snapshot", "mean_d", "%>40", "%>60"))
        f40 = []
        for i, (sid, _p) in enumerate(fr):
            d = dist[i]
            r40 = float((d > 40).mean())
            print("%-28s %8.2f %7.2f%% %7.2f%%"
                  % (sid, float(d.mean()), r40 * 100, float((d > 60).mean()) * 100))
            f40.append(r40)
        f40.sort()
        print("  frac>40  min=%.4f  median=%.4f  max=%.4f"
              % (f40[0], f40[len(f40) // 2], f40[-1]))


if __name__ == "__main__":
    import sys
    _diff_experiment(sys.argv[1:] or ["underA1", "underA5", "underA9", "underA16"])

