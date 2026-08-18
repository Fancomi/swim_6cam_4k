"""水面/纵向网格重叠标定叠图：入水机位原图上的透明参考线。

这是入水检测链路的**最后一步**：在交付给算法/标注的相机原图上，把两套标定网格
（水面 + 纵向，来自 fbx_overlay 提取的 mesh.json）画成一组透明参考线，让下游能
对着真实泳道核对泳道边界、深度与距离刻度。叠图是 RGBA 透明 PNG，可叠加在任何
1280×720 的相机图上，也可以先合成好再输出。

两套网格（water_entry 的两条线，见 python/fbx_overlay/profiles.py）：

  water_entry_a  006.fbx/Plane004   纵向网格（vertical）——泳道壁，画 0.0~1.5m 深度
  water_entry_b  005.fbx/Plane005   水面网格（surface）——水面，画水道边界与纵向刻度

四组线，画在**带 meter 的顶点**上（每组线用的顶点全部有对应轴 meter，验证见
tests/python/test_water_entry_overlay.py）：

  1. 水面网格横向线（meter.y 带，0m / 2.5m 两带，每带两条边）——0-1 左水线与
     3-4 右水线两条涂「黄色填充」：即画面最左、最右两条横向线围成的两条水线带。
     以 mesh 为准：横线的左右端点就是网格的左右边界，0-1 与 3-4 是边界两段。
  2. 水面网格纵向线（meter.x 列，0.5m 起）——每条画红色线段，只画水道范围内：
     跳过最右列（右水线，x meter 0.5m 是水面网格最右列），从下一列开始。
  3. 纵向网格横向线（meter.y 行 0.0~1.5m）——绿色线段。
  4. 纵向网格纵向线（meter.x 列）——蓝色线段，画到该列 meter 顶端。

方向与画法都以 mesh.json 为准（「有则画，所有线段都画在点上」）：线 = 同 meter
顶点之间实际存在的网格边；每组的范围、跳过规则按 meters.py 的列/行口径。

对齐：可传入 `--align-to` 现场新拍的图像，复用 python.align 的漂移修正——先对
参考图（标定图）求矩阵再作用到顶点 UV 上，再画。不给则直接用默认配对图像。
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

from python.align import cache as align_cache
from python.align.aligner import DEFAULT_MODEL, MODELS
from python.align.mesh import warp_uv
from python.common.media import MediaError, read_image, write_image
from python.common.paths import INPUTS, OUTPUTS

SIZE = (1280, 720)          # 入水机位相机原图尺寸

# 颜色（BGR）。黄 = 水线带填充；红 = 水面纵向刻度线；绿 = 纵向网格深度线；
# 蓝 = 纵向网格距离刻度线。
YELLOW = (0, 215, 255)
RED = (0, 0, 255)
GREEN = (0, 255, 0)
BLUE = (255, 0, 0)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)

# 纵向网格横向线（深度）画到的最大米数 —— 任务指定的 0.0~1.5m。
DEPTH_MAX_M = 1.5


class OverlayError(ValueError):
    """叠图输入不完整或不可用；消息面向用户。"""


def load_mesh_document(camera_dir):
    """一个相机的 mesh.json → {kind: mesh}；缺文件/缺 kind 报错。"""
    path = Path(camera_dir) / "mesh.json"
    if not path.is_file():
        raise OverlayError(f"mesh.json 缺失: {path}（先跑 python -m python.fbx_overlay）")
    document = json.loads(path.read_text(encoding="utf-8"))
    meshes = {}
    for mesh in document.get("meshes", []):
        kind = mesh.get("kind")
        if kind is None:
            raise OverlayError(f"{path} 里有 mesh 没有 kind: {mesh.get('node')}")
        meshes[kind] = mesh
    return meshes


def default_image(camera_dir, camera_name="water_entry_b"):
    """默认配对图像：跟 fbx_overlay 一致的 base image（标定图）。

    每条线、每个子相机各有自己的标定图：water_entry（旧 005/006）用
    background.jpg；water_entry2（femto/gemini）的全屏矩形纹理就是相机原图，
    即 fbx_overlay 的 `calibration_image`。这里直接按 line/camera 落 inputs
    路径（数据即文档，不猜）；缺了就报错，让调用方显式给 --image。"""
    candidates = []
    if camera_name.startswith("water_entry"):
        candidates.append(INPUTS / "water_entry" / "background.jpg")
    else:
        # water_entry2：femto/gemini 的 FBX 全屏矩形纹理 = 相机原图
        candidates.append(INPUTS / "water_entry" / "models" /
                          f"{camera_name}.fbm" /
                          (f"xlj_aux_orbbec_{camera_name}_1_mask_merged.png"
                           if camera_name == "femto"
                           else f"{camera_name}_camera_1_mask_merged.png"))
    return next((p for p in candidates if p.is_file()), None)


def reference_image_for(vertical_dir, camera_name="water_entry_b"):
    """对齐用的标定图：该相机 mesh 的标定帧。

    纵向网格的 UV 就是照这张图标的，所以它是 solve(标定图, 新图) 的 reference。
    取不到就报错，让调用方显式给 --image / --align-to。"""
    return default_image(vertical_dir, camera_name)


def meter_edges(mesh, axis, meters=None):
    """(axis, meter) → 该 meter 线全部网格边的集合（uv 对，已去重）。

    一条 meter 线 = 该 meter 的所有顶点之间实际存在的网格边。画线画的就是这些
    边；用「边」而不是「连成的链」是为了避免网格剖分在 meter 线交点处把一条线
    裂成多条重叠链、重复画线（同一 meter 的顶点可能同时属于两条相邻 meter 线，
    三角剖分在那里度数>2，链式连接会分叉）。
    """
    edges = defaultdict(set)
    for triangle in mesh["triangles"]:
        for i in range(3):
            a, b = triangle[i], triangle[(i + 1) % 3]
            ma, mb = a.get("meter"), b.get("meter")
            if (ma and mb and axis in ma and axis in mb
                    and abs(ma[axis] - mb[axis]) < 1e-9
                    and (meters is None or ma[axis] in meters)):
                edge = frozenset((tuple(a["uv"]), tuple(b["uv"])))
                edges[ma[axis]].add(edge)
    return {meter: [tuple(e) for e in eset] for meter, eset in edges.items()}


def draw_segments(canvas, edges, colour, thickness, alpha=0.85):
    """把若干 uv 线段画到 RGBA 画布上。`edges` 是 [(uv0, uv1), ...]。"""
    for (u0, v0), (u1, v1) in edges:
        p0 = (int(round(u0 * (SIZE[0] - 1))), int(round((1 - v0) * (SIZE[1] - 1))))
        p1 = (int(round(u1 * (SIZE[0] - 1))), int(round((1 - v1) * (SIZE[1] - 1))))
        cv2.line(canvas, p0, p1, colour + (int(255 * alpha),), thickness,
                 cv2.LINE_AA)


def fill_band(canvas, edges, colour, alpha=0.35):
    """把一组线段围成的带填充成半透明色。

    `edges` 是一条 y 带的两条边（横向线）的网格边集合。水面网格的横向线是泳道
    宽度方向的线，一条带的两条边围成一个带状多边形（水线带）。取边的全部顶点
    按画布坐标围成多边形填充；顶点顺序由 fillConvexPoly 处理凸包，横向线近似
    水平、围成的带近似凸。
    """
    pts = []
    seen = set()
    for (a, b) in edges:
        for uv in (a, b):
            key = (round(uv[0], 4), round(uv[1], 4))
            if key not in seen:
                seen.add(key)
                pts.append((int(round(uv[0] * (SIZE[0] - 1))),
                            int(round((1 - uv[1]) * (SIZE[1] - 1)))))
    if len(pts) < 3:
        return
    cv2.fillConvexPoly(canvas, np.array(pts, np.int32),
                       colour + (int(255 * alpha),), cv2.LINE_AA)


def uv_edges_on(mesh, axis, meter):
    """同 meter 且两端都有该轴 meter 的网格边（uv 对集合）。"""
    out = set()
    for triangle in mesh["triangles"]:
        for i in range(3):
            a, b = triangle[i], triangle[(i + 1) % 3]
            ma, mb = a.get("meter"), b.get("meter")
            if (ma and mb and axis in ma and axis in mb
                    and abs(ma[axis] - meter) < 1e-9
                    and abs(mb[axis] - meter) < 1e-9):
                out.add((tuple(a["uv"]), tuple(b["uv"])))
    return out


def chains(mesh, axis, meters=None):
    """(axis, meter) 线 → 折线列表（同 meter 的边连成串）。

    网格是三角剖分，一条 meter 线由若干三角形共享边组成；这里按共享顶点把它们
    连成连续折线（非流形分叉处断开）。返回 {meter: [[(u,v), ...], ...]}。
    """
    by_meter = defaultdict(set)
    for triangle in mesh["triangles"]:
        for i in range(3):
            a, b = triangle[i], triangle[(i + 1) % 3]
            ma, mb = a.get("meter"), b.get("meter")
            if (ma and mb and axis in ma and axis in mb
                    and abs(ma[axis] - mb[axis]) < 1e-9
                    and (meters is None or ma[axis] in meters)):
                by_meter[ma[axis]].add((tuple(a["uv"]), tuple(b["uv"])))
    result = {}
    for meter, edges in by_meter.items():
        adjacency = defaultdict(set)
        for a, b in edges:
            adjacency[a].add(b)
            adjacency[b].add(a)
        used = set()
        polylines = []
        for a, b in edges:
            if (a, b) in used or (b, a) in used:
                continue
            poly = [a, b]
            used.add((a, b))
            used.add((b, a))
            for _ in range(1000):
                nxt = [n for n in adjacency[poly[0]]
                       if (n, poly[0]) not in used and (poly[0], n) not in used
                       and n not in poly]
                if not nxt:
                    break
                n = nxt[0]
                poly.insert(0, n)
                used.add((n, poly[0]))
                used.add((poly[0], n))
            for _ in range(1000):
                nxt = [n for n in adjacency[poly[-1]]
                       if (n, poly[-1]) not in used and (poly[-1], n) not in used
                       and n not in poly]
                if not nxt:
                    break
                n = nxt[0]
                poly.append(n)
                used.add((n, poly[-2]))
                used.add((poly[-2], n))
            polylines.append(poly)
        result[meter] = polylines
    return result


def meter_values(mesh, axis):
    """mesh 里出现过的某轴 meter 值（去重、升序）。"""
    values = set()
    for triangle in mesh["triangles"]:
        for vertex in triangle:
            meter = vertex.get("meter")
            if meter and axis in meter:
                values.add(round(meter[axis], 3))
    return sorted(values)


def _anchor_uv(vertical, axis, meter, other_axis):
    """纵向网格某 meter 线在另一轴最外侧的顶点 uv（侧标锚点）。

    右侧标（高度）用 meter.y 行在**右列**（meter.x 最小）上的顶点；上侧标
    （横向）用 meter.x 列在**顶行**（meter.y 最大）上的顶点。取该 meter 线上
    other_axis meter 值最极端的那个顶点——它同时带两个轴的 meter，所以既在
    线上又在边上。"""
    best = None
    for triangle in vertical["triangles"]:
        for vertex in triangle:
            meter_v = vertex.get("meter")
            if (meter_v and axis in meter_v and other_axis in meter_v
                    and abs(meter_v[axis] - meter) < 1e-9):
                if best is None or meter_v[other_axis] < best[1]:
                    best = (vertex["uv"], meter_v[other_axis])
    return best[0] if best else None


def _put_label(canvas, text, uv, side, font_scale=0.6, thickness=2,
               colour=WHITE):
    """在 uv 锚点旁写一段文字；`side` = "right"（右侧）或 "top"（上方）。

    透明画布上直接写文字，白字带黑色描边，保证任何底图上都读得清。"""
    x = int(round(uv[0] * (SIZE[0] - 1)))
    y = int(round((1 - uv[1]) * (SIZE[1] - 1)))
    if side == "right":
        origin = (min(x + 6, SIZE[0] - 1), y + 4)
    else:
        origin = (x, max(y - 6, 0))
    cv2.putText(canvas, text, origin, cv2.FONT_HERSHEY_SIMPLEX,
                font_scale, BLACK + (255,), thickness + 2, cv2.LINE_AA)
    cv2.putText(canvas, text, origin, cv2.FONT_HERSHEY_SIMPLEX,
                font_scale, colour + (255,), thickness, cv2.LINE_AA)


def draw_side_labels(canvas, vertical):
    """纵向网格右侧写高度米标（0.0~1.25m），上侧写横向米标（0.5m 起）。

    右侧 = meter.x 最小列（右水线列）；上侧 = meter.y 最大行（顶行）。锚点取
    该 meter 线在另一轴上最外侧的顶点——它同时带两个轴的 meter。water_entry
    与 water_entry2 的纵向网格都适用（旧线 0.0~1.25m 六行，新线同）。"""
    xm = set()
    ym = set()
    for triangle in vertical["triangles"]:
        for vertex in triangle:
            meter = vertex.get("meter")
            if meter:
                if "x" in meter:
                    xm.add(meter["x"])
                if "y" in meter:
                    ym.add(meter["y"])
    xm, ym = sorted(xm), sorted(ym)
    right = xm[0] if xm else None
    top = ym[-1] if ym else None
    # 右侧高度标：每个 y meter 一行（y 升序，从上往下？画面坐标 y 向下，
    # 米标从 0 到 1.25 由下往上排——直接按 meter 值排，锚点自带位置）。
    for y_meter in ym:
        anchor = _anchor_uv(vertical, "y", y_meter, "x") if right is not None else None
        if anchor:
            _put_label(canvas, f"{y_meter:g}m", anchor, "right")
    # 上侧横向标：每个 x meter 一列（按 x 升序，从右往左排）。
    for x_meter in xm:
        anchor = _anchor_uv(vertical, "x", x_meter, "y") if top is not None else None
        if anchor:
            _put_label(canvas, f"{x_meter:g}m", anchor, "top")


def build_overlay(surface, vertical, out_path, image=None, align=None):
    """画透明叠图，落盘 RGBA PNG；返回 (overlay, composite) 两幅。

    `align` 是 python.align 的解（Alignment 或 None）——非 None 时先作用到两个
    mesh 的 UV 上再画（画的是「修正后」的网格）。
    """
    if align is not None and align.accepted:
        surface = warp_uv(surface, align.H)
        vertical = warp_uv(vertical, align.H)
    elif align is not None:
        print(f"  对齐未采纳（{align.reason}），用原标定画")

    canvas = np.zeros((SIZE[1], SIZE[0], 4), np.uint8)

    # ---- (1) 水面网格横向线：0-1 左水线、3-4 右水线 黄色填充 ----
    # 横向线 = meter.y 带（0m / 2.5m 两带，每带两条边）。带内两条边就是该带的
    # 左右两条横向线；「0-1 左水线」与「3-4 右水线」= 最左/最右两条带边，
    # 各与相邻带边围成水线带。以 mesh 为准：y 带升序取第一条（左水线带）与
    # 最后一条（右水线带）。
    y_meters = meter_values(surface, "y")
    if len(y_meters) < 2:
        raise OverlayError(f"水面网格的横向线不足两条: {y_meters}")
    y_edges = meter_edges(surface, "y")
    # 横向线 = y 带（0m / 2.5m 两带，每带两条边）。「0-1 左水线」与「3-4 右水线」
    # = 最左/最右两条横向线，即第一带与最后一带：以 mesh 为准，把这两带的两条边
    # 围成的带填黄。
    fill_band(canvas, y_edges.get(y_meters[0], []), YELLOW)
    fill_band(canvas, y_edges.get(y_meters[-1], []), YELLOW)
    # 所有横向线（两条带的所有边）描成黄色轮廓
    for meter in y_meters:
        draw_segments(canvas, y_edges.get(meter, []), YELLOW, 2)

    # ---- (2) 水面网格纵向线：红色，跳过最右列（右水线），只画水道内 ----
    x_meters = meter_values(surface, "x")          # 升序 0.5..5.0
    lane_x = x_meters[:-1] if len(x_meters) > 1 else x_meters   # 跳过 0.5（最右=右水线）
    for meter in lane_x:
        draw_segments(canvas, meter_edges(surface, "x", meters={meter}).get(meter, []),
                      RED, 2)

    # ---- (3) 纵向网格横向线：绿色，0.0~1.5m 深度 ----
    depth_meters = [m for m in meter_values(vertical, "y") if m <= DEPTH_MAX_M]
    for meter in depth_meters:
        draw_segments(canvas, meter_edges(vertical, "y", meters={meter}).get(meter, []),
                      GREEN, 2)

    # ---- (4) 纵向网格纵向线：蓝色，画到该列 meter 顶端 ----
    for meter in meter_values(vertical, "x"):
        draw_segments(canvas, meter_edges(vertical, "x", meters={meter}).get(meter, []),
                      BLUE, 2)

    # ---- (5) 纵向网格侧标：右侧高度米标 + 上侧横向米标 ----
    draw_side_labels(canvas, vertical)

    write_image(out_path, canvas, "overlay")
    composite = None
    if image is not None:
        composite = image.copy()
        alpha = canvas[..., 3:4].astype(np.float32) / 255.0
        composite = (composite.astype(np.float32) * (1 - alpha)
                     + canvas[..., :3].astype(np.float32) * alpha)
        composite = composite.astype(np.uint8)
        write_image(out_path.with_suffix(".composite.png"), composite, "composite")
    return canvas, composite


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m python.water_entry.overlay",
        description="把水面/纵向标定网格画成透明叠图，作用于入水机位相机原图",
        epilog="产物: outputs/water_entry/calib/overlay/<line>/overlay.png (+ .composite.png)")
    parser.add_argument("--line", choices=("water_entry", "water_entry2"),
                        default="water_entry",
                        help="标定线（默认 %(default)s）")
    parser.add_argument("--camera", default=None,
                        help="water_entry2 的子相机（femto/gemini）；缺省全部")
    parser.add_argument("--overlay-dir", type=Path, default=None,
                        help="输出目录（默认 outputs/water_entry/calib/overlay/<line>）")
    parser.add_argument("--mesh-dir", type=Path, default=None,
                        help="mesh.json 所在目录（默认 outputs/<line>/overlay/<cam>）")
    parser.add_argument("--image", type=Path, default=None,
                        help="目标图像（现场新拍）；缺省用配对图像（标定图）")
    parser.add_argument("--align-to", type=Path, default=None,
                        help="对这张新图做相机微动修正（python.align）后再画")
    parser.add_argument("--align-model", default=DEFAULT_MODEL,
                        choices=tuple(MODELS))
    parser.add_argument("--no-cache", action="store_true",
                        help="不读/写对齐缓存")
    return parser.parse_args(argv)


def _camera_specs(line):
    """(camera_name, mesh_dir) 列表。

    water_entry 旧线：surface 与 vertical 是**两个 FBX、两个目录**（005/006）；
    water_entry2：每相机一个 FBX、目录里两者都有。旧线按「一个目录 = 一个网格」，
    返回 (camera, dir) 对，main 再按角色配对。"""
    if line == "water_entry":
        return [("water_entry_b",
                 OUTPUTS / "water_entry" / "overlay" / "water_entry_b"),
                ("water_entry_a",
                 OUTPUTS / "water_entry" / "overlay" / "water_entry_a")]
    return [(cam, OUTPUTS / "water_entry2" / "overlay" / cam)
            for cam in ("femto", "gemini")]


def _pair_meshes(line, specs, mesh_dir_arg):
    """按角色把 (camera, dir) 凑成 (surface_dir, vertical_dir, camera_name) 列表。

    旧线：specs 里一个是 surface 目录、一个是 vertical 目录，camera 名取 vertical
    那个（对齐与命名都以纵向网格为准）。新线：每个 spec 自己带两个网格。"""
    pairs = []
    if line == "water_entry":
        surface_dir = mesh_dir_arg or dict(specs)["water_entry_b"]
        vertical_dir = mesh_dir_arg or dict(specs)["water_entry_a"]
        pairs.append((surface_dir, vertical_dir, "water_entry_a"))
    else:
        for camera_name, mesh_dir in specs:
            pairs.append((mesh_dir_arg or mesh_dir,
                          mesh_dir_arg or mesh_dir, camera_name))
    return pairs


def main(argv=None):
    args = parse_args(argv)
    specs = _camera_specs(args.line)
    if args.camera:
        specs = [s for s in specs if s[0] == args.camera]
    if not specs:
        raise OverlayError(f"--line {args.line} 没有相机 {args.camera!r}")

    pairs = _pair_meshes(args.line, specs, args.mesh_dir)
    multi = len(pairs) > 1
    for surface_dir, vertical_dir, camera_name in pairs:
        surface = load_mesh_document(surface_dir).get("surface")
        vertical = load_mesh_document(vertical_dir).get("vertical")
        if surface is None or vertical is None:
            raise OverlayError(
                f"{camera_name} 需要水面网格(surface)与纵向网格(vertical)；"
                f"水面来自 {surface_dir}/mesh.json，纵向来自 {vertical_dir}/mesh.json。"
                "请先跑 python -m python.fbx_overlay")
        out_dir = args.overlay_dir or (OUTPUTS / "water_entry" / "calib"
                                       / "overlay" / args.line)
        out_dir.mkdir(parents=True, exist_ok=True)

        # 目标图像：--image 优先；否则 --align-to 给的图；再否则默认配对图（标定图）。
        image = None
        if args.image is not None:
            image = read_image(args.image, "target image")
        elif args.align_to is not None:
            image = read_image(args.align_to, "target image")

        alignment = None
        if args.align_to is not None:
            from python.align import cache as align_cache
            from python.align.probe import probe
            reference = reference_image_for(vertical_dir, camera_name)
            if reference is None:
                raise OverlayError(
                    f"{camera_name} 没有找到默认标定图，无法对齐"
                    "（请给 --image 或 --align-to）")
            cache_path = None if args.no_cache else out_dir / "align.json"
            solved = align_cache.resolve(
                args.line, [camera_name],
                {camera_name: read_image(reference)},
                {camera_name: probe(args.align_to)}, model=args.align_model,
                cache_path=cache_path)
            alignment = solved[camera_name]

        suffix = f"_{camera_name}" if multi else ""
        build_overlay(surface, vertical, out_dir / f"overlay{suffix}.png",
                      image=image, align=alignment)
        print(f"wrote {out_dir / f'overlay{suffix}.png'}")
        if image is not None:
            print(f"wrote {out_dir / f'overlay{suffix}.composite.png'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
