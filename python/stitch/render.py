"""Still images for one line: composite, grid diagnostic, span map, fusion heatmap.

The four outputs come from one pass over the same layers, because a grid drawn
from a second projection would show rounding differences that are not really
there. A line carrying a lane schematic (`label_line`) gets a fifth, the
schematic's ink stamped on the composite.

Why the span map is its own file rather than more ink on the grid: the grid answers
"is the geometry right" and is already dense (sixteen sets of triangle edges),
while the span map answers "which camera covers this stretch, and where does it
hand over". Overlaying the second on the first buried both.
"""
import cv2

from python.common.media import read_image, write_image
from python.stitch import compose as C
from python.stitch.profiles import StepError


def render(profile, tex_dir, out_prefix, ppm=None, blend_px=None,
           full_res=None, tex_names=None, grid=True, heatmap=True, spans=True):
    """Write `<out_prefix>{,_grid,_spans,_heat,_label}.png`. Returns (width, height)."""
    tex_dir = profile.still_textures if tex_dir is None else tex_dir
    if not tex_dir.is_dir():
        raise StepError(f"texture directory missing: {tex_dir}")

    meshes = C.load_meshes(profile.mesh_json, neg_v=profile.neg_v,
                          neg_u=profile.neg_u)
    names = tex_names or [mesh["texture_basename"] for mesh in meshes]
    if len(names) != len(meshes):
        raise StepError(f"{len(names)} texture names for {len(meshes)} meshes")
    textures = [read_image(tex_dir / name, "texture") for name in names]

    full_res = profile.full_res if full_res is None else full_res
    if ppm is None:
        # full_res adapts the density to the tallest source image so the still
        # ends up at native scale; otherwise the profile's baked density is what
        # the runtime honours and the still should match it.
        ppm = (C.adaptive_ppm(meshes, max(t.shape[0] for t in textures))
               if full_res else profile.ppm)
    blend_px = profile.blend_px if blend_px is None else blend_px

    canvas = C.Canvas(meshes, ppm, margin=profile.still_margin)
    print(f"canvas {canvas.width}x{canvas.height} @ {ppm:.2f}px/m")
    layers = [C.build_remap(mesh, canvas, (t.shape[1], t.shape[0]),
                            clip=profile.clip_uv)
              for mesh, t in zip(meshes, textures)]
    weights = C.blend_weights([layer[2] for layer in layers], blend_px)
    composite = C.composite(layers, [w[..., None] for w in weights],
                            [t.astype("float32") for t in textures], canvas)

    images = [(f"{out_prefix}.png", composite, cv2.INTER_LINEAR, "still")]
    if grid:
        images.append((f"{out_prefix}_grid.png",
                       C.draw_grid(composite.copy(), meshes, canvas),
                       cv2.INTER_LINEAR, "grid"))
    if spans:
        # A separate product from the grid, because the two answer different
        # questions and stacking them buries both: the grid shows the mesh, this
        # shows which camera covers which stretch of the panorama and where the
        # blends are. Drawn before the crop/rescale below so the bars travel with
        # the pixels they describe.
        images.append((f"{out_prefix}_spans.png",
                       C.draw_spans(composite.copy(), C.lane_spans(weights),
                                    list(profile.camera_ids)),
                       cv2.INTER_LINEAR, "span map"))
    if heatmap:
        # Nearest for the heatmap: interpolating between two lanes' hues invents a
        # third colour that reads as a transition band where there is none.
        images.append((f"{out_prefix}_heat.png",
                       C.fusion_heatmap(weights, canvas),
                       cv2.INTER_NEAREST, "heatmap"))
    if profile.label_line is not None:
        # Only the lines that HAVE a lane schematic (overhead*) get this one, and
        # it goes on the plain composite: the question is whether the designer's
        # distance marks land on the lane the photograph shows.
        images.append((f"{out_prefix}_label.png",
                       C.draw_label_line(composite, profile.label_line),
                       cv2.INTER_LINEAR, "label line"))

    # Trim the ragged uncovered rows BEFORE rescaling, so the rescale never
    # stretches black. Only meaningful with full_res, which is what defines a
    # height to rescale to.
    crop = (C.bottom_dirty_rows(C.union_coverage(layers, canvas))
            if full_res else 0)
    if crop:
        target = max(t.shape[0] for t in textures)
        images = [(path, C.crop_and_scale(image, crop, target, interpolation),
                   interpolation, kind)
                  for path, image, interpolation, kind in images]
        height, width = images[0][1].shape[:2]
        print(f"cropped bottom {crop}px -> scaled to {width}x{height}")

    for path, image, _interpolation, kind in images:
        write_image(path, image, kind)
        print(f"wrote {kind} {path}")
    height, width = images[0][1].shape[:2]
    return width, height
