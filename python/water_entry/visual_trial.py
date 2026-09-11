"""Render assistant-authored cord coordinates for visual calibration review.

This is a separate experimental path, not an automatic image estimator. The
JSON records the visual decisions; rerunning only reproduces their rendering.
Run with --help for inputs. No FBX SDK or existing calibration is required.
"""
import argparse
import hashlib
import json
import platform
import re
from pathlib import Path
from shutil import copy2

import numpy as np
import PIL
from PIL import Image, ImageDraw, ImageFont

from python.common.paths import OUTPUTS
from python.fbx_tools.write_surface import write_surface
from python.water_entry.visual_metrics import write_metrics


def source_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_inputs(document, source):
    """Reject stale annotations before writing any artifact."""
    if document['schema_version'] != 1:
        raise ValueError('Unsupported annotation schema')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', document['dataset_id']):
        raise ValueError('dataset_id must contain only letters, digits, _ or -')
    if len(document['size']) != 2 or any(type(v) is not int or v <= 0 for v in document['size']):
        raise ValueError('size must be two positive integer pixel dimensions')
    if not np.isfinite(document['lane_width_m']) or document['lane_width_m'] <= 0:
        raise ValueError('lane_width_m must be positive')
    for key in ('far_boundary', 'near_boundary'):
        boundary = np.asarray(document[key], dtype=float)
        if boundary.ndim != 2 or boundary.shape[1] != 2 or len(boundary) < 2 or not np.isfinite(boundary).all():
            raise ValueError(f'Invalid {key}')
        if not np.all(np.diff(boundary[:, 0]) > 0):
            raise ValueError(f'{key} must run left to right')
    if len(document['boundary_half_width_px']) != 2 or any(v <= 0 for v in document['boundary_half_width_px']):
        raise ValueError('Two positive boundary half widths required')
    if not document['label_offsets_px']:
        raise ValueError('label_offsets_px must not be empty')
    rows = document['lines']
    if len(rows) < 2 or not np.all(np.diff([r['metres'] for r in rows]) > 0):
        raise ValueError('At least two lines in strictly increasing metre order required')
    names = [r['file'] for r in rows]
    if len(set(names)) != len(names) or document['base_image'] not in names:
        raise ValueError('Unique source files and an annotated base_image required')
    for row in rows:
        name = row['file']
        if Path(name).name != name or '/' in name or '\\' in name:
            raise ValueError('Source file must be a basename')
        if source_hash(source/name) != row['sha256']:
            raise ValueError(f'Source fingerprint mismatch: {name}; visually retrace this dataset')
        with Image.open(source/name) as im:
            if im.size != tuple(document['size']):
                raise ValueError(f'Source dimensions differ: {name}')
        samples = np.asarray(row['samples'], dtype=float)
        if samples.shape != (2, 2) or not np.isfinite(samples).all() or np.linalg.norm(samples[1]-samples[0]) < 1:
            raise ValueError(f'Two distinct finite sample points required: {name}')
        if row['confidence'] not in ('high', 'medium', 'low'):
            raise ValueError(f'Unknown confidence: {name}')


def select_annotations(source, catalog=None):
    """Match complete image sets by content, never by directory name alone."""
    catalog = catalog or Path(__file__).with_name('visual_trials')
    files = {p.name: source_hash(p) for p in source.iterdir()
             if p.is_file() and p.suffix.lower() in ('.jpg', '.jpeg', '.png')}
    matches = []
    for path in catalog.glob('*.json'):
        document = json.loads(path.read_text(encoding='utf-8'))
        if files == {r['file']: r.get('sha256') for r in document['lines']}:
            matches.append(document)
    if len(matches) != 1:
        raise ValueError('No unique saved annotation matches this image set. '
                         'Follow docs/prompts/water_entry_visual.md to visually trace it, '
                         'then pass --annotations <new.json>.')
    return matches[0]


def intersection(samples, boundary):
    """Intersect the traced cord with a piecewise linear lane centreline.

    End segments extend beyond the image: clipping UVs would silently invent
    measured world positions at the image edge.
    """
    p, q = np.asarray(samples, dtype=float)
    for i, (a, b) in enumerate(zip(boundary, boundary[1:])):
        a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
        t, s = np.linalg.solve(np.column_stack((q-p, a-b)), a-p)
        if (s >= 0 or i == 0) and (s <= 1 or i == len(boundary)-2):
            return (p + t*(q-p)).tolist()
    raise ValueError('Cord has no lane intersection')


def render(document, output, source):
    validate_inputs(document, source)
    if output.resolve() == source.resolve() or output.resolve() in source.resolve().parents:
        raise ValueError('Output must not be the source directory or its ancestor')
    # Calculate and check topology before creating a partial delivery.
    crossings = [(intersection(r['samples'], document['far_boundary']),
                  intersection(r['samples'], document['near_boundary']))
                 for r in document['lines']]
    for far, near in crossings:
        if not np.isfinite([far, near]).all() or near[1] <= far[1]:
            raise ValueError('Selected near boundary must be below far boundary')
    for (f0, n0), (f1, n1) in zip(crossings, crossings[1:]):
        if (f1[0]-f0[0])*(n1[0]-n0[0]) <= 0:
            raise ValueError('Crossed or collapsed distance lines; review sample points')
    output.mkdir(parents=True, exist_ok=True)
    w, h = document['size']
    scale = 3
    layer = Image.new('RGBA', (w*scale, h*scale))
    draw = ImageDraw.Draw(layer)
    font = ImageFont.load_default(size=12*scale)
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">']
    vertices, uv, triangles, records = [], [], [], []
    for line_index, row in enumerate(document['lines']):
        far, near = crossings[line_index]
        draw.line([tuple(v*scale for v in p) for p in (far, near)], fill='#ff0000', width=2*scale)
        svg.append(f'<path d="M {far[0]} {far[1]} L {near[0]} {near[1]}" fill="none" stroke="red" stroke-width="2"/>')
        # Two rows below the selected lane keep dense labels off the cord.
        offsets = document['label_offsets_px']
        label_y = near[1] + offsets[line_index % len(offsets)]
        label_x = max(28, min(w-28, near[0]))
        leader = [(near[0], near[1]+document['boundary_half_width_px'][1]),
                  (label_x, label_y-7)]
        draw.line([tuple(v*scale for v in p) for p in leader], fill='red', width=scale)
        svg.append(f'<path d="M {leader[0][0]} {leader[0][1]} L {leader[1][0]} {leader[1][1]}" fill="none" stroke="red" stroke-width="1"/>')
        label = f"{row['metres']:.2f}m"
        draw.text((label_x*scale, label_y*scale), label, font=font, anchor='mm', fill='red', stroke_width=scale, stroke_fill='white')
        svg.append(f'<text x="{label_x}" y="{label_y}" text-anchor="middle" dominant-baseline="middle" font-family="sans-serif" font-size="12" fill="red" stroke="white" stroke-width="2" paint-order="stroke">{label}</text>')
        for y, point in ((0, far), (document['lane_width_m'], near)):
            vertices.append([row['metres'], y, 0])
            uv.append([point[0]/w, 1-point[1]/h])
        records.append(dict(row, far_px=far, near_px=near,
                            sha256=hashlib.sha256((source/row['file']).read_bytes()).hexdigest()))
    for boundary, half in zip((document['far_boundary'], document['near_boundary']), document['boundary_half_width_px']):
        draw.line([tuple(v*scale for v in p) for p in boundary], fill='yellow', width=2*half*scale)
        points = ' '.join(f'{x},{y}' for x,y in boundary)
        svg.append(f'<polyline points="{points}" fill="none" stroke="yellow" stroke-width="{half*2}"/>')
    for i in range(len(records)-1):
        a = i*2
        triangles.extend([[a,a+2,a+1],[a+1,a+2,a+3]])
    layer = layer.resize((w,h), Image.Resampling.LANCZOS)
    layer.save(output/'overlay.png')
    svg.append('</svg>')
    (output/'overlay.svg').write_text('\n'.join(svg), encoding='utf-8')
    white = Image.new('RGBA',(w,h),'white')
    Image.alpha_composite(white,layer).convert('RGB').save(output/'overlay.white.png')
    base = Image.open(source/document['base_image']).convert('RGBA')
    if base.size != (w,h):
        raise ValueError('Source image dimensions differ from annotation')
    Image.alpha_composite(base,layer).convert('RGB').save(output/'overlay.composite.png')
    mesh = dict(status=document['status'], scope=document['scope'],
                lane_index_from_camera=document['lane_index_from_camera'],
                axes='X=distance from wall, Y=far to near lane boundary, Z=up; metres',
                lane_width_status=document['lane_width_status'], vertices=vertices, uv=uv,
                triangles=triangles, observations=records)
    (output/'surface.json').write_text(json.dumps(mesh,indent=2),encoding='utf-8')
    (output/'annotations.json').write_text(json.dumps(document,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    write_surface(output/'surface.trial.fbx',vertices,uv,triangles)
    review_dir = output/'sources'
    review_dir.mkdir(exist_ok=True)
    for row in records:
        copy2(source/row['file'],review_dir/row['file'])
    review_data = dict(dataset_id=document['dataset_id'], size=document['size'],
                       lane=document['lane_index_from_camera'], status=document['status'],
                       lane_width=document['lane_width_m'], width_status=document['lane_width_status'],
                       distance_interpretation=document['distance_interpretation'], rows=records)
    template = Path(__file__).with_name('visual_review.html').read_text(encoding='utf-8')
    # JSON is embedded for file:// use; escape '<' so filenames cannot end script.
    payload = json.dumps(review_data, ensure_ascii=True).replace('<', '\\u003c')
    (output/'index.html').write_text(template.replace('__REVIEW_DATA__', payload),encoding='utf-8')
    report = dict(schema_version=1, dataset_id=document['dataset_id'],
                  source_fingerprints_verified=True, line_count=len(records),
                  vertex_count=len(vertices), triangle_count=len(triangles),
                  all_uv_in_frame=bool(np.all((np.asarray(uv) >= 0) & (np.asarray(uv) <= 1))),
                  low_confidence_files=[r['file'] for r in records if r['confidence']=='low'],
                  environment=dict(python=platform.python_version(), pillow=PIL.__version__, numpy=np.__version__))
    report['rgba_sha256'] = {name: hashlib.sha256(Image.open(output/name).convert('RGBA').tobytes()).hexdigest()
                             for name in ('overlay.png','overlay.white.png','overlay.composite.png')}
    report['file_sha256'] = {name: source_hash(output/name) for name in ('overlay.svg','surface.trial.fbx')}
    reference = document.get('reference_render')
    report['reference_matches'] = ({key: {name: report[key].get(name)==value for name,value in reference[key].items()}
                                    for key in ('rgba_sha256','file_sha256')} if reference else None)
    (output/'verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    write_metrics(document, source, output)
    print(output.resolve())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--annotations', type=Path, help='Saved visual samples; omitted only for a fingerprint-matched dataset')
    parser.add_argument('--output', type=Path, help='Default: outputs/water_entry/calib/visual_trial_<dataset_id>')
    args = parser.parse_args()
    document = (json.loads(args.annotations.read_text(encoding='utf-8'))
                if args.annotations else select_annotations(args.source))
    output = args.output or OUTPUTS/'water_entry/calib'/f"visual_trial_{document['dataset_id']}"
    render(document,output,args.source)


if __name__ == '__main__':
    main()
