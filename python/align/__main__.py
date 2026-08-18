"""Camera-drift correction: run the calibration-x-dataset matrix and report.

    ./scripts/run_align.sh                       # every cell
    ./scripts/run_align.sh --only underwater     # one line's cells
    ./scripts/run_align.sh --dry-run             # print the plan

The point of a matrix rather than a single run: a correction can only be judged
against the alternatives it is competing with. Each cell fixes a CALIBRATION and a
DATASET, and renders the same pixels twice — once with the calibration as
delivered, once corrected — so the pair differs in nothing but the UVs.

    underwater   x 202607   the old calibration on data from its own era: the
                            floor, what the geometry is worth with no drift
    underwater   x 202608   the same calibration six weeks later: the drift case,
                            and the one the field actually looks like
    underwater2  x 202608   the re-surveyed calibration on its own era's data:
                            what a fresh hand calibration achieves
    water_entry  x 0807     the old femto UVs on a new frame — and because
                            water_entry2/femto is that same geometry
                            hand-recalibrated on this very frame, the correction
                            can be scored against ground truth
    water_entry2 x 0807     that ground truth itself, as the control

Cells are data (see CELLS below), which is where to add one. The two kinds run
through different chains — a stitch line is scored by whether neighbours agree on
their seams, a water-entry camera by how far its UVs moved — so each kind has its
own runner, and both write into one summary.
"""
import argparse
from dataclasses import dataclass
from pathlib import Path

from python.align import cache as align_cache
from python.align import seams as SEAMS
from python.align.aligner import DEFAULT_MODEL, MODELS
from python.align.mesh import uv_shift_px, warp_uv
from python.common import page, tables
from python.common.media import MediaError
from python.common.paths import OUTPUTS, dataset_root

UNDER_VIDEOS = "SWIM_UNDER_VIDEOS_ROOT"
UNDER_VIDEOS_DEFAULT = ("/Users/penghaotian/Downloads/DATAS/SWIMMING/"
                        "swimming-xlj-under-videos")
GRIDS = "SWIM_UNDER_GRIDS_ROOT"
GRIDS_DEFAULT = "/Users/penghaotian/Downloads/DATAS/SWIMMING/swimming-xlj-under-grids"


def _clip_dir(month, sample):
    return dataset_root(UNDER_VIDEOS, UNDER_VIDEOS_DEFAULT) / month / sample


def _frame(date, name):
    return dataset_root(GRIDS, GRIDS_DEFAULT) / date / "object-frames" / name


@dataclass(frozen=True)
class Cell:
    """One (calibration, dataset) pair to solve, render and score.

    `key` names the cell's output directory and its alignment cache, so the same
    line solved against two datasets keeps two answers instead of overwriting one.
    `camera` is set for the overlay lines only, which align one named camera
    against one named image rather than a whole line against a directory."""
    name: str
    kind: str                       # "stitch" | "overlay"
    line: str
    key: str
    note: str
    source: object                  # a directory (stitch) or an image (overlay)
    camera: str | None = None


# Data, not code: adding a cell is adding a record. Ordered so the report reads
# as an argument — the no-drift floor first, then the drift, then the remedy.
CELLS = (
    Cell(name="underwater-202607", kind="stitch", line="underwater",
         key="202607_swb_20260730-162514_12",
         note="old calibration, same-era data (drift floor)",
         source=_clip_dir("202607", "swb_20260730-162514_12")),
    Cell(name="underwater-202608", kind="stitch", line="underwater",
         key="202608_swb_20260813-170549_24",
         note="old calibration, six weeks later (the drift case)",
         source=_clip_dir("202608", "swb_20260813-170549_24")),
    Cell(name="underwater2-202608", kind="stitch", line="underwater2",
         key="202608_swb_20260813-170549_24",
         note="re-surveyed calibration, same-era data",
         source=_clip_dir("202608", "swb_20260813-170549_24")),
    Cell(name="water_entry-0807-femto", kind="overlay", line="water_entry",
         key="20260807_femto", camera="water_entry_a",
         note="old femto UVs on a 20260807 frame (scored against water_entry2)",
         source=_frame("20260807", "xlj_aux_orbbec_femto_1_background.png")),
    Cell(name="water_entry2-0807-femto", kind="overlay", line="water_entry2",
         key="20260807_femto", camera="femto",
         note="ground truth: the same geometry recalibrated on this frame",
         source=_frame("20260807", "xlj_aux_orbbec_femto_1_background.png")),
    Cell(name="water_entry2-0807-gemini", kind="overlay", line="water_entry2",
         key="20260807_gemini", camera="gemini",
         note="control: calibration and frame from the same session",
         source=_frame("20260807", "gemini_camera_1_background.png")),
)

SUMMARY_COLUMNS = ["cell", "kind", "line", "key", "note", "cameras", "accepted",
                   "seam_mean_off", "seam_mean_on", "seam_min_off",
                   "seam_min_on", "seams_better", "seams_worse",
                   "uv_shift_px", "truth_err_off", "truth_err_on"]


def cell_dir(cell):
    return OUTPUTS / cell.line / "align" / cell.key


def _blank_row(cell):
    row = {column: "" for column in SUMMARY_COLUMNS}
    row.update(cell=cell.name, kind=cell.kind, line=cell.line, key=cell.key,
               note=cell.note)
    return row


def run_stitch_cell(cell, model, use_cache, force):
    """One stitch cell: solve the drift, render off/on, score both.

    Both renders are driven by the SAME probe images — the medians the alignment
    was solved against — rather than by whatever the profile's texture directory
    holds. That is what makes the pair a controlled comparison: identical pixels,
    identical canvas, the UVs the only difference.
    """
    from python.stitch import align as SA, profiles as SP, render as SR

    profile = SP.get(cell.line)
    alignments, probes, _key = SA.resolve(
        profile, cell.source, model=model, key=cell.key, use_cache=use_cache,
        force=force)
    textures = [probes.get(camera) for camera in profile.camera_ids]
    missing = [camera for camera, texture in zip(profile.camera_ids, textures)
               if texture is None]
    if missing:
        raise MediaError(f"no probe image for {', '.join(missing)} in "
                         f"{cell.source}")

    out = cell_dir(cell)
    meshes = SR.C.load_meshes(profile.mesh_json, neg_v=profile.neg_v,
                              neg_u=profile.neg_u)
    warped = SR.warp_meshes(meshes, alignments)
    # Score at the profile's own density, which is what the seam blending and
    # the runtime asset are shaped for; the stills below may rescale for viewing.
    before = SEAMS.seam_scores(meshes, profile, textures)
    after = SEAMS.seam_scores(warped, profile, textures)
    off = SEAMS.summarise(before)
    on = SEAMS.summarise(after, baseline=before)
    print(f"  seams off: {SEAMS.format_summary(off)}")
    print(f"  seams on : {SEAMS.format_summary(on)}")

    # full_res off for BOTH renders. It trims the ragged uncovered bottom rows
    # and rescales to source height, and the correction changes how many rows are
    # ragged — measured 67px off against 65px on, so the pair came out
    # 6571x720 and 6551x720 and could no longer be flipped between or diffed.
    # The uncropped canvas is identical by construction, which is the whole point
    # of the pair.
    SR.render(profile, None, out / "stitch_off", textures=textures,
              full_res=False)
    SR.render(profile, None, out / "stitch_on", textures=textures,
              alignments=alignments, full_res=False)

    solved = {camera: alignment for camera, alignment
              in zip(profile.camera_ids, alignments)}
    tables.write_rows(out / "cameras.csv", align_cache.REPORT_COLUMNS,
                      align_cache.report_rows(profile.camera_ids, solved))
    shifts = [abs(a.shift_px[0]) + abs(a.shift_px[1])
              for a in alignments if a is not None and a.accepted]
    row = _blank_row(cell)
    row.update(cameras=len(profile.camera_ids),
               accepted=sum(1 for a in alignments if a is not None and a.accepted),
               seam_mean_off=f"{off['mean']:.4f}", seam_mean_on=f"{on['mean']:.4f}",
               seam_min_off=f"{off['min']:.3f}", seam_min_on=f"{on['min']:.3f}",
               seams_better=on["better"], seams_worse=on["worse"],
               uv_shift_px=f"{max(shifts):.1f}" if shifts else "0.0")
    return row, [out / "stitch_off.png", out / "stitch_on.png",
                 out / "stitch_off_grid.png", out / "stitch_on_grid.png"]


def _truth_meshes(line, camera):
    """The hand-recalibrated counterpart of a water-entry mesh, or None.

    `water_entry/water_entry_a` and `water_entry2/femto` are the SAME geometry —
    vertex positions match to the bit — recalibrated by hand on a later frame, so
    the second line's UVs are ground truth for correcting the first's. Matched by
    kind rather than node name: the rebuild renamed Plane004 to Plane006."""
    truth = {("water_entry", "water_entry_a"): ("water_entry2", "femto")}
    pair = truth.get((line, camera))
    if pair is None:
        return None
    path = OUTPUTS / pair[0] / "overlay" / pair[1] / "mesh.json"
    if not path.is_file():
        print(f"  (no ground truth yet: run the overlay for {pair[0]}/{pair[1]})")
        return None
    import json
    return {mesh["kind"]: mesh
            for mesh in json.loads(path.read_text(encoding="utf-8"))["meshes"]}


def run_overlay_cell(cell, model, use_cache, force):
    """One water-entry cell: correct one camera's UVs against one new frame.

    Scored two ways. Always: how far the UVs moved, which says the correction is
    doing something of the right magnitude. Where a hand-recalibrated twin exists
    (`_truth_meshes`): the distance from the corrected UVs to the truth's, before
    and after — the only place in this repo where a drift correction can be graded
    rather than merely compared.
    """
    from python.fbx_overlay import align as OA, profiles as OP
    from python.fbx_overlay.classify import MeshKind, classify_mesh
    from python.fbx_overlay.render import draw_meshes
    from python.fbx_tools import scene as fbx_scene
    from python.common.media import write_image

    profile = OP.get(cell.line)
    camera = next(spec for spec in profile.cameras if spec.name == cell.camera)
    manager, _scene, nodes = fbx_scene.read_scene(camera.fbx)
    try:
        meshes = [fbx_scene.extract_mesh(node, camera.fbx.with_suffix(".fbm"))
                  for node in nodes]
    finally:
        manager.Destroy()
    for mesh in meshes:
        mesh["kind"] = classify_mesh(mesh)

    reference = OA.calibration_image(camera, meshes)

    out = cell_dir(cell)
    alignment, probe_image = OA.resolve(
        cell.line, camera.name, reference, cell.source, model=model,
        cache_path=(out / "align.json") if use_cache else None, force=force)

    drawn = [mesh for mesh in meshes if mesh["kind"] is not MeshKind.FULL_FRAME]
    colours = [(0, 255, 255), (0, 128, 255), (60, 200, 60)][:len(drawn)]
    write_image(out / "overlay_off.png",
                draw_meshes(probe_image, drawn, colors=colours,
                            v_origin=camera.v_origin, thickness=2),
                "overlay")
    corrected = drawn
    if alignment is not None and alignment.accepted:
        corrected = [warp_uv(mesh, alignment.H) for mesh in drawn]
        write_image(out / "overlay_on.png",
                    draw_meshes(probe_image, corrected, colors=colours,
                                v_origin=camera.v_origin, thickness=2),
                    "overlay")

    size = (probe_image.shape[1], probe_image.shape[0])
    moved = max((uv_shift_px(before, after, size)[0]
                 for before, after in zip(drawn, corrected)), default=0.0)
    row = _blank_row(cell)
    row.update(cameras=1,
               accepted=1 if alignment is not None and alignment.accepted else 0,
               uv_shift_px=f"{moved:.1f}")

    truth = _truth_meshes(cell.line, camera.name)
    if truth is not None:
        errors_off, errors_on = [], []
        for before, after in zip(drawn, corrected):
            reference_mesh = truth.get(before["kind"].value)
            if reference_mesh is None:
                continue
            try:
                errors_off.append(uv_shift_px(before, reference_mesh, size)[0])
                errors_on.append(uv_shift_px(after, reference_mesh, size)[0])
            except ValueError as error:
                # A rebuilt mesh with a different triangle count is not the same
                # geometry, so it cannot grade this one. Say so instead of
                # silently reporting the one kind that did match.
                print(f"  ({before['kind'].value}: no comparable truth — "
                      f"{error})")
        if errors_off:
            row.update(truth_err_off=f"{max(errors_off):.1f}",
                       truth_err_on=f"{max(errors_on):.1f}")
            print(f"  vs ground truth: {max(errors_off):.1f}px -> "
                  f"{max(errors_on):.1f}px")

    products = [out / "overlay_off.png"]
    if (out / "overlay_on.png").is_file():
        products.append(out / "overlay_on.png")
    return row, products


PAGE_CSS = """
table{border-collapse:collapse;margin:8px 0 20px;font-size:12px}
th,td{border:1px solid #2c313a;padding:4px 8px;text-align:right}
th:first-child,td:first-child,td.note{text-align:left}
td.note{color:#8d97a5}
.pair{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.pair figure img{width:auto;max-width:100%}
"""


def write_report(rows, products):
    """One page holding the matrix table and every cell's off/on pair.

    A CSV as well, because the numbers are what gets quoted, and a page because
    the numbers alone cannot show whether a seam actually closed."""
    summary = tables.write_rows(OUTPUTS / "align" / "summary.csv",
                               SUMMARY_COLUMNS, rows)
    header = "".join(f"<th>{page.escape(column)}</th>"
                     for column in SUMMARY_COLUMNS)
    body = ["<h1>Camera-drift correction: calibration x dataset</h1>",
            '<p class="meta">Each cell renders the same probed pixels twice — '
            "off is the calibration as delivered, on is the same calibration "
            "with its UVs corrected. Seam scores are the NCC of neighbouring "
            "cameras inside their overlap; truth_err grades the water-entry "
            "correction against a hand-recalibrated twin.</p>",
            f"<table><tr>{header}</tr>"]
    for row in rows:
        cells = "".join(
            f'<td class="note">{page.escape(str(row[column]))}</td>'
            if column in ("cell", "line", "key", "note", "kind")
            else f"<td>{page.escape(str(row[column]))}</td>"
            for column in SUMMARY_COLUMNS)
        body.append(f"<tr>{cells}</tr>")
    body.append("</table>")
    for row in rows:
        images = products.get(row["cell"], [])
        if not images:
            continue
        body.append(f"<h2>{page.escape(row['cell'])}</h2>")
        body.append(f'<p class="meta">{page.escape(row["note"])}</p>')
        body.append('<div class="pair">')
        for image in images:
            try:
                relative = image.relative_to(OUTPUTS / "align")
            except ValueError:
                # Cells write under outputs/<line>/align/; link back out.
                relative = Path("..") / image.relative_to(OUTPUTS)
            body.append(
                f'<figure><img data-src="{page.escape(relative.as_posix())}" '
                f'alt="{page.escape(image.name)}">'
                f"<figcaption>{page.escape(image.name)}</figcaption></figure>")
        body.append("</div>")
    path = page.write_page(OUTPUTS / "align" / "index.html",
                          "Camera-drift correction", body, css=PAGE_CSS,
                          cell_width=900)
    print(f"\nwrote {summary}")
    print(f"wrote {path}")
    return summary, path


RUNNERS = {"stitch": run_stitch_cell, "overlay": run_overlay_cell}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m python.align",
        description="Solve camera drift and render the calibration x dataset "
                    "matrix",
        epilog="cells: " + ", ".join(cell.name for cell in CELLS))
    parser.add_argument("--only", action="append", metavar="CELL_OR_LINE",
                        help="run only cells whose name or line matches; "
                             "may be repeated")
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        choices=tuple(MODELS),
                        help="drift transform to fit (default: %(default)s)")
    parser.add_argument("--no-cache", action="store_true",
                        help="solve every camera instead of reusing cached "
                             "alignments")
    parser.add_argument("--force", action="store_true",
                        help="re-solve even when the cache matches")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the cells that would run and stop")
    return parser.parse_args(argv)


def selected(cells, only):
    if not only:
        return list(cells)
    wanted = set(only)
    return [cell for cell in cells
            if cell.name in wanted or cell.line in wanted]


def main(argv=None):
    args = parse_args(argv)
    cells = selected(CELLS, args.only)
    if not cells:
        raise SystemExit(f"no cell matches {args.only}; valid: "
                         + ", ".join(cell.name for cell in CELLS))
    if args.dry_run:
        for cell in cells:
            print(f"{cell.name:26s} {cell.kind:8s} {cell.line:13s} "
                  f"{cell.key:34s} <- {cell.source}")
        return 0

    rows, products, failed = [], {}, []
    for cell in cells:
        print(f"\n=== {cell.name} ({cell.note}) ===", flush=True)
        if not Path(cell.source).exists():
            print(f"skipped: {cell.source} does not exist")
            failed.append(cell.name)
            continue
        try:
            row, made = RUNNERS[cell.kind](cell, args.model, not args.no_cache,
                                           args.force)
        except (MediaError, OSError, ValueError) as error:
            # One unusable cell must not cost the others: the matrix is the
            # product, and a partial matrix with a named gap is still readable.
            print(f"failed: {error}")
            failed.append(cell.name)
            continue
        rows.append(row)
        products[row["cell"]] = made
    if rows:
        write_report(rows, products)
    if failed:
        print(f"\n{len(failed)} cell(s) did not run: {', '.join(failed)}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
