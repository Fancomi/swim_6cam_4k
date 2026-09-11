"""Visual records must not silently follow stale images or old session names."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from python.water_entry.visual_trial import render, select_annotations, source_hash


class VisualTrialTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root/'images'
        self.source.mkdir()
        for name in ('new-025.png', 'new-050.png'):
            Image.new('RGB', (320, 180), 'navy').save(self.source/name)
        self.document = dict(
            schema_version=1, dataset_id='other_session', size=[320, 180],
            base_image='new-050.png', status='pending visual review',
            lane_index_from_camera=2, lane_width_m=2.5, lane_width_status='assumed',
            scope='second lane', distance_interpretation='filename / 100 metres',
            far_boundary=[[0, 80], [320, 80]], near_boundary=[[0, 120], [320, 120]],
            boundary_half_width_px=[3, 4], label_offsets_px=[18, 34],
            lines=[dict(file=name, metres=metres, samples=samples, confidence='high',
                        sha256=source_hash(self.source/name))
                   for name, metres, samples in (
                       ('new-025.png', .25, [[180, 90], [190, 110]]),
                       ('new-050.png', .50, [[140, 90], [145, 110]]))])

    def run_render(self, path, document=None):
        with contextlib.redirect_stdout(io.StringIO()):
            render(document or self.document, path, self.source)

    def test_fresh_delivery_uses_current_dataset_and_is_repeatable(self):
        a, b = self.root/'a', self.root/'b'
        self.run_render(a)
        self.run_render(b)
        for name in ('overlay.png', 'overlay.svg', 'surface.trial.fbx', 'index.html'):
            self.assertEqual((a/name).read_bytes(), (b/name).read_bytes())
        page = (a/'index.html').read_text(encoding='utf-8')
        self.assertIn('other_session', page)
        self.assertNotIn('0909', page)
        self.assertNotIn('__REVIEW_DATA__', page)
        self.assertTrue((a/'sources/new-050.png').is_file())
        with Image.open(a/'overlay.png') as im:
            self.assertEqual(im.size, (320, 180))
            self.assertEqual(im.mode, 'RGBA')
            self.assertEqual(im.getchannel('A').getextrema(), (0, 255))
        report = json.loads((a/'verification.json').read_text())
        self.assertEqual(report['line_count'], 2)
        self.assertIsNone(report['reference_matches'])

    def test_changed_source_fails_before_writing(self):
        Image.new('RGB', (320, 180), 'red').save(self.source/'new-025.png')
        with self.assertRaisesRegex(ValueError, 'fingerprint mismatch'):
            self.run_render(self.root/'delivery')
        self.assertFalse((self.root/'delivery').exists())

    def test_catalog_match_requires_complete_image_set(self):
        catalog = self.root/'catalog'
        catalog.mkdir()
        (catalog/'record.json').write_text(json.dumps(self.document), encoding='utf-8')
        self.assertEqual(select_annotations(self.source, catalog), self.document)
        Image.new('RGB', (320, 180)).save(self.source/'extra.png')
        with self.assertRaisesRegex(ValueError, 'No unique saved annotation'):
            select_annotations(self.source, catalog)

    def test_crossing_lines_fail_before_writing(self):
        self.document['lines'][1]['samples'] = [[160, 90], [220, 110]]
        with self.assertRaisesRegex(ValueError, 'Crossed'):
            self.run_render(self.root/'crossed')
        self.assertFalse((self.root/'crossed').exists())

    def test_embedded_filename_cannot_end_html_script(self):
        self.document['status'] = '</script><script>alert(1)</script>'
        self.run_render(self.root/'escaped')
        page = (self.root/'escaped/index.html').read_text(encoding='utf-8')
        self.assertNotIn(self.document['status'], page)
        self.assertIn(r'\u003c/script>', page)


if __name__ == '__main__':
    unittest.main()
