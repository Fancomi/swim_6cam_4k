"""Local support must expose displacement without inventing evidence on flat water."""
import unittest

import numpy as np

from python.water_entry.visual_metrics import cord_support


class VisualMetricsTest(unittest.TestCase):
    def test_shifted_bright_cord_has_better_supported_candidate(self):
        gray = np.full((80, 100), 40.0)
        gray[38:41, :] = 220
        points = [[20, 43], [70, 43]]
        result = cord_support(gray, points)
        self.assertIn('nearby_stronger_trace', result['flags'])
        self.assertGreater(result['contrast_gain_gray'], 100)
        for x, y in result['candidate']['samples']:
            self.assertLessEqual(abs(y-39), 1)
        self.assertEqual(points, [[20, 43], [70, 43]])

    def test_flat_image_remains_weak(self):
        result = cord_support(np.full((80, 100), 40.0), [[20, 40], [70, 40]])
        self.assertIn('weak_image_support', result['flags'])
        self.assertEqual(result['contrast_gain_gray'], 0)
        self.assertEqual(result['current']['support_fraction'], 0)

    def test_image_edge_is_unmeasurable(self):
        result = cord_support(np.zeros((80, 100)), [[20, 1], [70, 1]])
        self.assertFalse(result['measurable'])


if __name__ == '__main__':
    unittest.main()
