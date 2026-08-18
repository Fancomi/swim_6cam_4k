"""Camera-drift correction: run the calibration-x-dataset matrix and report.

    ./scripts/run_align.sh                       # every cell
    ./scripts/run_align.sh --only underwater     # one line's cells
    ./scripts/run_align.sh --video 4             # also render off/on panoramas
    ./scripts/run_align.sh --dry-run             # print the plan

A correction can only be judged against the alternatives it competes with, so
this runs a CROSS: each rig has two calibration versions and two eras of data,
and every combination is a cell. Each cell renders the same pixels twice — once
with the calibration as delivered, once corrected — differing in nothing but the
UVs.

               data era 1              data era 2
    cal v1     same-era: the floor     CROSS: v1 drifted onto era-2 data
    cal v2     CROSS: v2 onto era-1    same-era: the floor

The two diagonal cells are the reference: a calibration on its own era's data is
what "no drift" looks like, and the aligner should barely move it. The two
off-diagonal cells are the experiment, and BOTH directions are run — a correction
that only worked forwards in time would be fitting the datasets rather than the
drift. The water-entry crosses are additionally graded against ground truth,
because each version's UVs ARE the truth for the other era: the two lines hold
the same geometry (vertex positions identical to the bit) recalibrated by hand on
their own era's frame.

    underwater   x 202607   cal v1 (all.fbx)  on its own era      — floor
    underwater   x 202608   cal v1 six weeks later                — CROSS
    underwater2  x 202607   cal v2 (8.15.fbx) on the older era    — CROSS (reverse)
    underwater2  x 202608   cal v2 on its own era                 — floor
    water_entry  x 0708     cal v1 on its own frame               — floor
    water_entry  x 0807     cal v1 on the newer frame             — CROSS, graded
    water_entry2 x 0708     cal v2 on the older frame             — CROSS (reverse), graded
    water_entry2 x 0807     cal v2 on its own frame               — floor
    water_entry2 x 0807-H   gemini: one calibration only, no cross to make

gemini cannot form a cross and is not pretending to: only one calibration version
of it exists (the 20260708 shoot has a `gemini_camera_1_merged.png`, but it is
the same pixels as the 0807 one — correlation 0.9994 — so there is no earlier
survey, just a copy). Its cell is the negative control instead: registered
against a DIFFERENT shoot of the same day, where the camera provably did not move,
the aligner must report ~0 and gain nothing. Measured 0.01~0.12px. A drift
corrector that cannot stay still is worse than none.

Cells are data (see CELLS below), which is where to add one. The two kinds run
through different chains — a stitch line is scored by whether neighbours agree on
their seams, a water-entry camera by how far its UVs moved and by ground truth —
so each kind has its own runner, and both write into one summary.
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


FEMTO_0708 = "orbbec_camera_1_background.png"      # the 0708 name for this camera
FEMTO_0807 = "xlj_aux_orbbec_femto_1_background.png"
GEMINI = "gemini_camera_1_background.png"


@dataclass(frozen=True)
class Cell:
    """One (calibration, dataset) pair to solve, render and score.

    `key` names the cell's output directory and its alignment cache, so the same
    line solved against two datasets keeps two answers instead of overwriting one.
    `camera` is set for the overlay lines only, which align one named camera
    against one named image rather than a whole line against a directory.
    `cross` marks the off-diagonal cells — the experiment, as against the
    same-era cells that establish what no drift looks like."""
    name: str
    kind: str                       # "stitch" | "overlay"
    line: str
    key: str
    note: str
    source: object                  # a directory (stitch) or an image (overlay)
    camera: str | None = None
    cross: bool = False


# Data, not code: adding a cell is adding a record. Ordered rig by rig, and
# within a rig cal-v1 then cal-v2, so the report reads as the 2x2 it is.
CELLS = (
    Cell(name="underwater-v1-202607", kind="stitch", line="underwater",
         key="202607_swb_20260730-162514_12",
         note="cal v1 on its own era — the no-drift floor",
         source=_clip_dir("202607", "swb_20260730-162514_12")),
    Cell(name="underwater-v1-202608", kind="stitch", line="underwater",
         key="202608_swb_20260813-170549_24", cross=True,
         note="CROSS: cal v1 six weeks later, the field case",
         source=_clip_dir("202608", "swb_20260813-170549_24")),
    Cell(name="underwater2-v2-202607", kind="stitch", line="underwater2",
         key="202607_swb_20260730-162514_12", cross=True,
         note="CROSS reversed: cal v2 back onto the older era",
         source=_clip_dir("202607", "swb_20260730-162514_12")),
    Cell(name="underwater2-v2-202608", kind="stitch", line="underwater2",
         key="202608_swb_20260813-170549_24",
         note="cal v2 on its own era — the no-drift floor",
         source=_clip_dir("202608", "swb_20260813-170549_24")),
    Cell(name="water_entry-v1-0708", kind="overlay", line="water_entry",
         key="20260708_femto", camera="water_entry_a",
         note="cal v1 on its own frame — the no-drift floor",
         source=_frame("20260708", FEMTO_0708)),
    Cell(name="water_entry-v1-0807", kind="overlay", line="water_entry",
         key="20260807_femto", camera="water_entry_a", cross=True,
         note="CROSS: cal v1 on the 0807 frame, graded against cal v2",
         source=_frame("20260807", FEMTO_0807)),
    Cell(name="water_entry2-v2-0708", kind="overlay", line="water_entry2",
         key="20260708_femto", camera="femto", cross=True,
         note="CROSS reversed: cal v2 on the 0708 frame, graded against cal v1",
         source=_frame("20260708", FEMTO_0708)),
    Cell(name="water_entry2-v2-0807", kind="overlay", line="water_entry2",
         key="20260807_femto", camera="femto",
         note="cal v2 on its own frame — the no-drift floor",
         source=_frame("20260807", FEMTO_0807)),
    Cell(name="gemini-v2-0807H", kind="overlay", line="water_entry2",
         key="20260807H_gemini", camera="gemini",
         note="negative control: only one gemini calibration exists, and this "
              "shoot's camera did not move — the aligner must report ~0",
         source=_frame("20260807-6cam-Horizontal", GEMINI)),
)

SUMMARY_COLUMNS = ["cell", "kind", "cross", "line", "key", "note", "cameras",
                   "accepted", "seam_mean_off", "seam_mean_on", "seam_min_off",
                   "seam_min_on", "seams_better", "seams_worse",
                   "uv_shift_px", "truth_err_off", "truth_err_on"]


def cell_dir(cell):
    return OUTPUTS / cell.line / "align" / cell.key


def _blank_row(cell):
    row = {column: "" for column in SUMMARY_COLUMNS}
    row.update(cell=cell.name, kind=cell.kind, line=cell.line, key=cell.key,
               note=cell.note, cross="1" if cell.cross else "")
    return row


def run_stitch_cell(cell, model, use_cache, force, video_seconds=None):
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
    products = [out / "stitch_off.png", out / "stitch_on.png",
                out / "stitch_off_grid.png", out / "stitch_on_grid.png"]
    if video_seconds:
        products += _render_videos(cell, profile, alignments, out, video_seconds)

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
    return row, products


def _render_videos(cell, profile, alignments, out, seconds):
    """The off/on pair as moving panoramas. Returns what it wrote.

    The still is a median background: it shows whether the STRUCTURE lines up, and
    that is the honest way to measure a correction. It cannot show what a viewer
    actually watches — a swimmer crossing a mis-registered seam jumps, stretches
    or briefly duplicates, and none of that is visible in a frame with no swimmer
    in it. So the video answers the other half of the question.

    Off by default (`--video N`): ~90s per version at 4 seconds of 16-lane 6000px
    panorama on this machine, against ~15s for the whole still pair. Both versions
    read the same clips through the same time alignment, so the two files are
    frame-for-frame comparable — only the UVs differ.
    """
    from python.stitch import render_video as RV

    written = []
    for tag, applied in (("off", None), ("on", alignments)):
        path = out / f"video_{tag}.mp4"
        print(f"  rendering {seconds:g}s of panorama -> {path.name}", flush=True)
        RV.render(profile, cell.source, path, seconds=seconds,
                  full_res=False, alignments=applied)
        written.append(path)
    return written


def _truth_meshes(line, camera):
    """The hand-recalibrated counterpart of a water-entry mesh, or None.

    `water_entry/water_entry_a` and `water_entry2/femto` are the SAME geometry —
    vertex positions match to the bit — surveyed by hand on two different eras'
    frames, so each one's UVs are ground truth for correcting the other's onto
    that era. The map runs BOTH ways for exactly that reason: the reverse cross
    (v2 dragged back onto the 0708 frame) is graded against v1, and a corrector
    that only worked forwards in time would be fitting the datasets rather than
    the drift. Matched by kind rather than node name — the rebuild renamed
    Plane004 to Plane006."""
    truth = {("water_entry", "water_entry_a"): ("water_entry2", "femto"),
             ("water_entry2", "femto"): ("water_entry", "water_entry_a")}
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


def run_overlay_cell(cell, model, use_cache, force, video_seconds=None):
    """One water-entry cell: correct one camera's UVs against one new frame.

    `video_seconds` is accepted and ignored — these cameras deliver single frames,
    not clips, so there is no panorama to animate. Taking the argument keeps the
    two runners callable through one table.

    Scored two ways. Always: how far the UVs moved, which says the correction is
    doing something of the right magnitude. Where a hand-surveyed twin of the
    other era exists (`_truth_meshes`): the distance from the corrected UVs to the
    truth's, before and after — the only place in this repo where a drift
    correction can be graded rather than merely compared.
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
    on_path = out / "overlay_on.png"
    if alignment is not None and alignment.accepted:
        corrected = [warp_uv(mesh, alignment.H) for mesh in drawn]
        write_image(on_path, draw_meshes(probe_image, corrected, colors=colours,
                                         v_origin=camera.v_origin, thickness=2),
                    "overlay")
    elif on_path.is_file():
        # A refusal this run must not leave an accepted run's image behind: the
        # report links by name, and a stale `on` would be read as this cell's.
        on_path.unlink()

    size = (probe_image.shape[1], probe_image.shape[0])
    moved = max((uv_shift_px(before, after, size)[0]
                 for before, after in zip(drawn, corrected)), default=0.0)
    row = _blank_row(cell)
    row.update(cameras=1,
               accepted=1 if alignment is not None and alignment.accepted else 0,
               uv_shift_px=f"{moved:.1f}")

    # Only a CROSS can be graded. On the diagonal the calibration's own UVs ARE
    # the truth for that frame, so scoring them against the other era's survey
    # measures nothing but the 50px distance between the two versions — it read
    # as "50.0px -> 50.1px, no improvement" and invited the conclusion that the
    # aligner had failed, when the right result there is the correction being
    # ~0px, which `uv_shift_px` already reports.
    truth = _truth_meshes(cell.line, camera.name) if cell.cross else None
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
tr.cross td{background:#1c2129}
.pair{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px}
.pair figure img,.pair figure video{width:auto;max-width:100%;display:block;
  border:2px solid #2c313a;border-radius:4px;background:#000}
"""


def _figure(path):
    """One off/on product as a figure, linked relative to the report."""
    try:
        relative = path.relative_to(OUTPUTS / "align")
    except ValueError:
        # Cells write under outputs/<line>/align/; link back out.
        relative = Path("..") / path.relative_to(OUTPUTS)
    source = page.escape(relative.as_posix())
    caption = page.escape(path.name)
    if path.suffix == ".mp4":
        # No lazy `data-src` for video: the loader only swaps <img>, and a browser
        # will not fetch a <video> until it is played anyway.
        media = (f'<video src="{source}" controls preload="metadata" '
                 f'muted loop></video>')
    else:
        media = f'<img data-src="{source}" alt="{caption}">'
    return f"<figure>{media}<figcaption>{caption}</figcaption></figure>"


def write_report(rows, products, partial=False):
    """One page holding the matrix table and every cell's off/on pair.

    A CSV as well, because the numbers are what gets quoted, and a page because
    the numbers alone cannot show whether a seam actually closed.

    A partial run REPLACES the summary with just the cells that ran, and says so
    at the top. Merging into the previous summary instead would be worse: rows
    from an older code version or a stale dataset would sit unmarked beside fresh
    ones, and the whole point of the matrix is that its rows are comparable."""
    summary = tables.write_rows(OUTPUTS / "align" / "summary.csv",
                               SUMMARY_COLUMNS, rows)
    header = "".join(f"<th>{page.escape(column)}</th>"
                     for column in SUMMARY_COLUMNS)
    body = ["<h1>Camera-drift correction: calibration x dataset</h1>"]
    if partial:
        body.append('<p class="meta"><b>Partial run:</b> only the cells below '
                    "were run, so this is a slice of the matrix rather than the "
                    "whole cross. Run without <code>--only</code> for the full "
                    "picture.</p>")
    body += ['<p class="meta">Each cell renders the same probed pixels twice — '
             "off is the calibration as delivered, on is the same calibration "
             "with its UVs corrected. The highlighted rows are the CROSSES "
             "(a calibration meeting the other era's data), which is the "
             "experiment; the rest are that calibration on its own era, which is "
             "what no drift looks like — there the right result is a correction "
             "of ~0px. Seam scores are the NCC of neighbouring cameras inside "
             "their overlap; truth_err grades a cross against the other era's "
             "hand survey of the same geometry, and is blank on the diagonal "
             "where the calibration's own UVs already ARE that frame's truth.</p>",
             f"<table><tr>{header}</tr>"]
    for row in rows:
        cells = "".join(
            f'<td class="note">{page.escape(str(row[column]))}</td>'
            if column in ("cell", "line", "key", "note", "kind")
            else f"<td>{page.escape(str(row[column]))}</td>"
            for column in SUMMARY_COLUMNS)
        body.append(f'<tr class="cross">{cells}</tr>' if row["cross"]
                    else f"<tr>{cells}</tr>")
    body.append("</table>")
    for row in rows:
        images = products.get(row["cell"], [])
        if not images:
            continue
        body.append(f"<h2>{page.escape(row['cell'])}</h2>")
        body.append(f'<p class="meta">{page.escape(row["note"])}</p>')
        body.append('<div class="pair">')
        body += [_figure(path) for path in images]
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
    parser.add_argument("--video", type=float, nargs="?", const=4.0,
                        default=None, metavar="SECONDS",
                        help="stitch cells: also render SECONDS of off/on "
                             "panorama video (default %(const)s when the flag "
                             "is bare). Off unless asked: ~90s per version "
                             "against ~15s for the still pair")
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
            print(f"{cell.name:24s} {'CROSS' if cell.cross else '     '} "
                  f"{cell.kind:8s} {cell.line:13s} {cell.key:34s} "
                  f"<- {cell.source}")
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
                                           args.force,
                                           video_seconds=args.video)
        except (MediaError, OSError, ValueError) as error:
            # One unusable cell must not cost the others: the matrix is the
            # product, and a partial matrix with a named gap is still readable.
            print(f"failed: {error}")
            failed.append(cell.name)
            continue
        rows.append(row)
        products[row["cell"]] = made
    if rows:
        write_report(rows, products, partial=len(rows) < len(CELLS))
    if failed:
        print(f"\n{len(failed)} cell(s) did not run: {', '.join(failed)}")
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
