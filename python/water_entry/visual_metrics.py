"""Measure local image support for visually selected cords, without changing points."""
import json

import numpy as np
from PIL import Image, ImageDraw


def sample_gray(gray, points):
    """Bilinear samples; callers keep the full profile inside the image."""
    x, y = points[..., 0], points[..., 1]
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    dx, dy = x-x0, y-y0
    return (gray[y0, x0]*(1-dx)*(1-dy) + gray[y0, x0+1]*dx*(1-dy)
            + gray[y0+1, x0]*(1-dx)*dy + gray[y0+1, x0+1]*dx*dy)


def cord_support(gray, samples):
    """Search only within six normal pixels of a human-selected segment.

    Contrast is evidence, not cord identity: reflections can score higher.
    Independent endpoint shifts expose angle errors as well as translations.
    """
    points = np.asarray(samples, dtype=float)
    tangent = points[1]-points[0]
    normal = np.array([-tangent[1], tangent[0]])/np.linalg.norm(tangent)
    t = np.linspace(0, 1, 41)[:, None]
    base = points[0]*(1-t)+points[1]*t
    offsets = np.array([-4, -3, -0.5, 0, 0.5, 3, 4])
    candidates = []
    for a in range(-6, 7):
        for b in range(-6, 7):
            centers = base + ((1-t)*a+t*b)*normal
            probe = centers[:, None, :] + offsets[None, :, None]*normal
            h, w = gray.shape
            if not ((probe[..., 0] >= 0).all() and (probe[..., 0] < w-1).all()
                    and (probe[..., 1] >= 0).all() and (probe[..., 1] < h-1).all()):
                continue
            values = sample_gray(gray, probe)
            contrast = values[:, 2:5].mean(axis=1)-values[:, [0, 1, 5, 6]].mean(axis=1)
            candidates.append(dict(endpoint_normal_offsets_px=[a, b],
                                   contrast_gray=float(np.median(contrast)),
                                   support_fraction=float(np.mean(contrast > 5)),
                                   samples=(points+np.array([a, b])[:, None]*normal).tolist()))
    current = next((c for c in candidates if c['endpoint_normal_offsets_px'] == [0, 0]), None)
    if current is None:
        return dict(measurable=False, reason='Profile extends outside image; visual review required')
    best = max(candidates, key=lambda c: (c['contrast_gray'],
               -sum(abs(v) for v in c['endpoint_normal_offsets_px'])))
    gain = best['contrast_gray']-current['contrast_gray']
    flags = []
    if current['contrast_gray'] < 5 or current['support_fraction'] < .6:
        flags.append('weak_image_support')
    if gain > 5 and max(abs(v) for v in best['endpoint_normal_offsets_px']) >= 2:
        flags.append('nearby_stronger_trace')
    if 6 in [abs(v) for v in best['endpoint_normal_offsets_px']]:
        flags.append('search_limit_reached')
    return dict(measurable=True, current=current, candidate=best,
                contrast_gain_gray=gain, flags=flags)


def write_metrics(document, source, output):
    """Write reproducible metrics and paired raw/marked crops for every frame."""
    directory = output/'review'
    directory.mkdir(exist_ok=True)
    rows = []
    for row in document['lines']:
        with Image.open(source/row['file']) as im:
            rgb = im.convert('RGB')
        gray = np.asarray(rgb.convert('L'), dtype=float)
        result = cord_support(gray, row['samples'])
        points = np.asarray(row['samples'])
        lo = np.maximum(np.floor(points.min(axis=0)-24), 0).astype(int)
        hi = np.minimum(np.ceil(points.max(axis=0)+24), rgb.size).astype(int)
        box = (*lo, *hi)
        raw = rgb.crop(box).resize(tuple((hi-lo)*6))
        marked = raw.copy()
        draw = ImageDraw.Draw(marked)
        for samples, color in [(row['samples'], 'cyan')] + (
                [(result['candidate']['samples'], 'magenta')] if result['measurable'] else []):
            draw.line([tuple((np.array(p)-lo)*6) for p in samples], fill=color, width=2)
        pair = Image.new('RGB', (raw.width*2, raw.height+24), 'white')
        pair.paste(raw, (0, 24)); pair.paste(marked, (raw.width, 24))
        ImageDraw.Draw(pair).text((4, 4), f"{row['file']} origin={tuple(map(int, lo))} scale=6 cyan=current magenta=candidate", fill='black')
        name = f"{row['metres']:.2f}.png"
        pair.save(directory/name)
        rows.append(dict(file=row['file'], metres=row['metres'], sha256=row['sha256'],
                         confidence=row['confidence'], crop=f'review/{name}', **result))
    report = dict(schema_version=1, method='local normal contrast v1',
                  warning='Image support is not ground truth or metric accuracy. Visually confirm all corrections.',
                  parameters=dict(radius_px=6, contrast_threshold_gray=5, support_threshold=.6),
                  rows=rows)
    (output/'review_metrics.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf-8')
    return report
