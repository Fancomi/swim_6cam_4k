"""Grid-to-meters mapping for one camera's FBX meshes.

The calibration meshes sit on real lane geometry, so every gridline carries a
real-world meter value. The rules are the anchor constants below plus a step
measured from the mesh's own geometry ("以网格为准"): only the anchors are
fixed (skip the rightmost column; skip the bottom/top rows for the vertical
mesh; water surface bands are 2.5 m apart), and the per-column/per-row spacing
is whatever the actual FBX says. That way an artist rebuild at a different
spacing still produces correct meters, and one code path serves both cameras.

Grid layout measured from femto/gemini:

    vertical  X columns step 0.5 m, Y rows step 0.25 m
    surface   X columns step 1.0 m, Y rows are two horizontal bands (2.5 m)

This module is pure (no FBX SDK, no OpenCV, no NumPy) so the mapping is
testable on a machine that only has Python.
"""
from collections import Counter

from .classify import MeshKind


# Rounding key shared with render.py's label lookup — a value grouped here must
# group identically there.
METER_PRECISION = 6
# X: the rightmost column is skipped ("右侧空一列"); 右2 = 0.5 m, then +step
# leftward. Both mesh kinds share this anchor; only the step differs.
VERTICAL_ANCHOR_X_M = 0.5
# Y (vertical): the bottom and top rows are skipped; 下1 = 0 m, then +step upward.
VERTICAL_ANCHOR_Y_M = 0.0
# Y (surface): the bottom band is 0 m and each band above adds the band pitch
# measured from the mesh ("以网格为准"). The designer's first model had two
# bands 2.5 m apart; a rebuild moved them to three bands 1.25 m apart — the
# pitch is measured, not a constant, so both work.
SURFACE_BAND_GAP_M = 1.0
# A y-gap >= this separates two horizontal bands. Measured within-band gaps are
# 0.2 m and between-band gaps 1.25 m (femto and gemini identical); 1.0 sits
# between them.


def _coords(mesh, axis):
    """Distinct values of vertex ``pos[axis]``, ascending.

    Adjacent values closer than a hair (1e-4 m) are merged: the FBX grids carry
    float noise (e.g. 33.814530 vs 33.814531 for the same row), which round()
    alone does not always collapse at the precision boundary.
    """
    values = sorted(
        round(vertex["pos"][axis], METER_PRECISION)
        for triangle in mesh.get("triangles", ())
        for vertex in triangle
    )
    merged = []
    for value in values:
        if not merged or value - merged[-1] > 1e-4:
            merged.append(value)
    return merged


def _step(values):
    """The regular spacing of `values`: most-common adjacent gap.

    A single missing column/row doubles one gap; the mode still finds the true
    step. Ties go to the smaller gap (a doubled gap is the larger candidate,
    and choosing small avoids over-stepping). 0.0 for fewer than 2 values.
    """
    gaps = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    if not gaps:
        return 0.0
    counts = Counter(round(gap, METER_PRECISION) for gap in gaps)
    return max(counts, key=lambda gap: (counts[gap], -abs(gap)))


def column_entries(mesh, kind=None, rightmost_x=None):
    """[{x, meter}] for one mesh's columns, left to right.

    Vertical/surface: the rightmost column is skipped ("右侧空一列"); 右2 = 0.5 m,
    then +step (measured from the mesh) leftward.

    Plane (overhead): NO skip — meter is the world-coordinate difference from
    the pool's rightmost X ("从右往左 0m 1m 2m..."). `rightmost_x` is pool-wide
    (across both planes); when omitted it falls back to this mesh's own max x,
    which is correct for the single rightmost plane and convenient for tests.
    """
    kind = _as_kind(kind, mesh)
    xs = _coords(mesh, 0)
    if kind is MeshKind.PLANE:
        rightmost = rightmost_x if rightmost_x is not None else (
            xs[-1] if xs else 0.0)
        return [
            {"x": x, "meter": round(rightmost - x, METER_PRECISION)}
            for x in xs
        ]
    kept = xs[:-1]                       # skip the rightmost column
    if not kept:
        return []
    step = _step(kept) if len(kept) >= 2 else 0.0
    count = len(kept)
    return [
        {"x": x, "meter": round(VERTICAL_ANCHOR_X_M + (count - 1 - i) * step,
                                METER_PRECISION)}
        for i, x in enumerate(kept)
    ]


def _as_kind(kind, mesh):
    """Resolve a kind from the explicit arg or the mesh's own 'kind' key."""
    if kind is not None:
        return kind if isinstance(kind, MeshKind) else MeshKind(kind)
    stored = mesh.get("kind")
    if stored is None:
        raise ValueError("mesh has no 'kind'; pass kind explicitly")
    return stored if isinstance(stored, MeshKind) else MeshKind(stored)


def _surface_bands(ys):
    """[[y, ...], ...] clustering `ys` into horizontal bands by gap.

    A band's rows are those whose neighbours are closer than SURFACE_BAND_GAP_M;
    a gap at or above the threshold starts a new band. Returns [] for no rows.
    """
    bands = []
    for y in ys:
        if bands and y - bands[-1][-1] < SURFACE_BAND_GAP_M:
            bands[-1].append(y)
        else:
            bands.append([y])
    return bands


def row_entries(mesh, kind=None):
    """[{y, meter}] for one mesh's rows, bottom to top.

    Vertical: skip the bottom and top rows; 下1 = 0 m, then +step each.
    Surface: cluster Y into horizontal bands by gap; the bottom band is 0 m and
    each band above adds the pitch measured between band midpoints (the artist
    may rebuild the model at a different spacing — the pitch follows the mesh).
    Both rows of a band share its meter.
    Plane (overhead): skip the bottom and top rows; 下1 = 0 m, then meter is the
    world-coordinate difference upward (irregular rows keep their true meters).
    """
    kind = _as_kind(kind, mesh)
    ys = _coords(mesh, 1)
    if kind is MeshKind.SURFACE:
        bands = _surface_bands(ys)
        if not bands:
            return []
        mids = [sum(band) / len(band) for band in bands]
        pitch = _step(mids) if len(mids) >= 2 else 0.0
        return [
            {"y": y, "meter": round(index * pitch, METER_PRECISION)}
            for index, band in enumerate(bands)
            for y in band
        ]
    if kind is MeshKind.PLANE:
        kept = ys[1:-1]                      # skip bottom and top rows
        if not kept:
            return []
        anchor = kept[0]                     # 下1 = 0 m
        return [
            {"y": y, "meter": round(y - anchor, METER_PRECISION)}
            for y in kept
        ]

    kept = ys[1:-1]                      # skip bottom and top rows
    if not kept:
        return []
    step = _step(kept) if len(kept) >= 2 else 0.0
    return [
        {"y": y, "meter": round(VERTICAL_ANCHOR_Y_M + index * step,
                                METER_PRECISION)}
        for index, y in enumerate(kept)
    ]


def grid_annotation(mesh, kind=None, rightmost_x=None):
    """{"x": [...], "y": [...]} for a VERTICAL, SURFACE or PLANE mesh.

    FULL_FRAME is a 2-triangle camera quad — it has no meaningful grid — and
    asking for one is a caller bug.
    """
    kind = _as_kind(kind, mesh)
    if kind is MeshKind.FULL_FRAME:
        raise ValueError(f"grid annotation is not defined for {kind.value}")
    return {"x": column_entries(mesh, kind, rightmost_x),
            "y": row_entries(mesh, kind)}


def pool_rightmost(meshes):
    """The largest world X across every mesh — the pool's right edge.

    For the overhead planes the X meters are pool-wide: Plane002 must read
    15-25 m (its -25.22 right edge is 15 m from the pool's -10.22 right end),
    not 0-10 m. Only meaningful for PLANE meshes; others ignore it.
    """
    xs = [v["pos"][0] for mesh in meshes for t in mesh["triangles"] for v in t]
    return max(xs) if xs else 0.0


def annotate_document(camera, source, meshes, rightmost_x=None):
    """The per-camera JSON document dict: {"source", "camera", "meshes"}.

    Each entry is a shallow copy of the extract_mesh dict plus "kind" (a plain
    string for JSON); its "triangles" are replaced by the vertex-annotated copy
    from ``inline_vertex_meters`` so each vertex carries its own meter. FULL_FRAME
    meshes are skipped — they only supply the base image. When any mesh is a
    PLANE, the pool-wide rightmost X is derived unless passed in. Pure and
    json.dumps-able.
    """
    has_plane = any(mesh["kind"] is MeshKind.PLANE for mesh in meshes)
    if rightmost_x is None and has_plane:
        rightmost_x = pool_rightmost(meshes)
    document = {"source": source, "camera": camera, "meshes": []}
    for mesh in meshes:
        kind = mesh["kind"]
        if kind is MeshKind.FULL_FRAME:
            continue
        entry = dict(mesh)
        entry["kind"] = kind.value
        entry["triangles"] = inline_vertex_meters(mesh, kind, rightmost_x)
        document["meshes"].append(entry)
    return document


def inline_vertex_meters(mesh, kind=None, rightmost_x=None):
    """Deep-copied triangles with each vertex carrying its meter when it has one.

    A vertex on a gridline with a meter (a kept column, a kept vertical row, a
    surface band row, a plane row) gets ``{"meter": {"x": ..., "y": ...}}`` —
    x from the column's meter, y from the row's/band's meter. A vertex on a
    SKIPPED gridline (the rightmost column of a vertical/surface mesh, the
    bottom/top rows) has no meter key at all: "有的话". The input mesh is not
    mutated — the renderer keeps using the raw triangles and the grid for label
    placement.
    """
    kind = _as_kind(kind, mesh)
    x_entries = {
        round(e["x"], METER_PRECISION): e["meter"]
        for e in column_entries(mesh, kind, rightmost_x)
    }
    y_entries = {
        round(e["y"], METER_PRECISION): e["meter"]
        for e in row_entries(mesh, kind)
    }
    out = []
    for triangle in mesh.get("triangles", ()):
        new_triangle = []
        for vertex in triangle:
            new = dict(vertex)
            meter = {}
            mx = x_entries.get(round(vertex["pos"][0], METER_PRECISION))
            my = y_entries.get(round(vertex["pos"][1], METER_PRECISION))
            if mx is not None:
                meter["x"] = mx
            if my is not None:
                meter["y"] = my
            if meter:
                new["meter"] = meter
            new_triangle.append(new)
        out.append(new_triangle)
    return out


def label_anchors_world(mesh, grid):
    """Deduplicated [(world_x, world_y, text, side)] for canvas-projected labels.

    Like ``label_anchors`` but in WORLD coordinates (pos[0]/pos[1]) instead of
    UV, for the overhead canvas renderer which projects world -> canvas pixels.
    X labels anchor at the column's top edge (max pos[1]); Y labels at the
    row's right end (max pos[0]), deduplicated by meter.
    """
    anchors = []
    if not grid:
        return anchors
    columns = {}
    rows = {}
    for triangle in mesh.get("triangles", ()):
        for vertex in triangle:
            x = round(vertex["pos"][0], METER_PRECISION)
            y = round(vertex["pos"][1], METER_PRECISION)
            columns.setdefault(x, []).append(vertex["pos"])
            rows.setdefault(y, []).append(vertex["pos"])
    for entry in grid.get("x", ()):
        points = columns.get(round(entry["x"], METER_PRECISION))
        if not points:
            continue
        wx = sum(p[0] for p in points) / len(points)
        wy = max(p[1] for p in points)           # the column's top edge
        anchors.append((wx, wy, f"{entry['meter']:g}", "above"))
    seen = set()
    for entry in grid.get("y", ()):
        meter = entry["meter"]
        if meter in seen:
            continue
        seen.add(meter)
        points = []
        for y, group in rows.items():
            if any(round(e["y"], METER_PRECISION) == y and e["meter"] == meter
                   for e in grid["y"]):
                points.extend(group)
        if not points:
            continue
        wx = max(p[0] for p in points)           # the row's right end
        wy = sum(p[1] for p in points) / len(points)
        anchors.append((wx, wy, f"{meter:g}", "left"))
    return anchors


def label_anchors(mesh, grid):
    """Deduplicated [(uv, text, side)] for one mesh's meter labels.

    Derived from the mesh's real geometry so labels land on the gridlines they
    name, and deduplicated so a meter that appears on several rows/bands (the
    water surface's two bands both carry the same X meters) is written once:

    - X labels: one per grid.x column, anchored at the vertex row with that
      world-X that lies on the mesh's widest band (largest UV-u span — for the
      surface that is the bottom band; for the vertical mesh every column has a
      single u, so the anchor is the column's own position). ``side="above"``.
    - Y labels: one per DISTINCT grid.y meter (a band's two edge rows share
      their meter), anchored at the row's RIGHT end on the widest band, so the
      row meters run down the right side of the image. ``side="left"``.

    Returns a list the renderer can draw directly; entries whose anchor has no
    matching vertex are dropped.
    """
    anchors = []
    if not grid:
        return anchors

    columns = {}
    rows = {}
    for triangle in mesh.get("triangles", ()):
        for vertex in triangle:
            x = round(vertex["pos"][0], METER_PRECISION)
            y = round(vertex["pos"][1], METER_PRECISION)
            columns.setdefault(x, []).append(vertex["uv"])
            rows.setdefault(y, []).append(vertex["uv"])

    def _widest(uv_groups):
        """The group of UVs with the largest u-span (the mesh's dominant band)."""
        return max(uv_groups, key=lambda uvs: max(uv[0] for uv in uvs)
                   - min(uv[0] for uv in uvs))

    for entry in grid.get("x", ()):
        uvs = columns.get(round(entry["x"], METER_PRECISION))
        if not uvs:
            continue
        u = sum(uv[0] for uv in uvs) / len(uvs)
        v = max(uv[1] for uv in uvs)             # top edge of the widest band
        anchors.append(([u, v], f"{entry['meter']:g}", "above"))

    seen_meters = set()
    for entry in grid.get("y", ()):
        meter = entry["meter"]
        if meter in seen_meters:
            continue                             # same band's other edge row
        seen_meters.add(meter)
        # All rows sharing this meter (the band's edge rows).
        matching = [uv for y, group in rows.items()
                    for uv in group
                    if any(round(e["y"], METER_PRECISION) == y
                           and e["meter"] == meter for e in grid["y"])]
        if not matching:
            continue
        v = sum(uv[1] for uv in matching) / len(matching)
        u = max(uv[0] for uv in matching)        # the band's right end
        anchors.append(([u, v], f"{meter:g}", "left"))

    return anchors
