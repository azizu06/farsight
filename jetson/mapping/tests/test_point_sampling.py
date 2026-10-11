"""Use the measured sample budget without inventing pixels or losing coverage."""

import math
import unittest

import numpy as np

from jetson.mapping.geometry import Calibration
from jetson.mapping.phone import RGBDFrame, measured_points


def frame(height=120, width=160):
    ids = np.arange(height * width, dtype=np.int32).reshape(height, width)
    colors = np.stack((ids % 256, ids // 256 % 256, ids // 65536), axis=-1).astype(np.uint8)
    return RGBDFrame({}, colors, np.ones((height, width), np.float32),
                     np.full((height, width), 2, np.uint8),
                     Calibration(width, height, 160, 160, width / 2, height / 2), np.eye(4), b'')


def pixel_ids(colors):
    rgb = colors.astype(np.int64)
    return rgb[:, 0] + rgb[:, 1] * 256 + rgb[:, 2] * 65536


class PointSamplingTests(unittest.TestCase):
    def test_full_budget_and_native_xyz_rgb_pairs_when_valid_count_is_just_above_cap(self):
        sample = frame()
        native, _ = measured_points(sample)
        for budget in (4096, 12288, 16384, len(native) - 1):
            with self.subTest(budget=budget):
                xyz, colors = measured_points(sample, max_points=budget)
                ids = pixel_ids(colors)
                self.assertEqual(len(xyz), budget)
                self.assertEqual(len(np.unique(ids)), budget)
                np.testing.assert_array_equal(xyz, native[ids])
                # Spread across the whole sensor; no concentrated patch of it.
                ordered = np.sort(ids)
                gaps = np.diff(np.append(ordered, ordered[0] + len(native)))
                self.assertLessEqual(gaps.max(), math.ceil(len(native) / budget))

    def test_rotating_phases_cover_every_valid_native_pixel(self):
        sample = frame(192, 256)
        count = sample.depth.size
        for budget in (4096, 12288, 16384, 32768):
            with self.subTest(budget=budget):
                seen = set()
                for phase in range(math.ceil(count / budget)):
                    _, colors = measured_points(sample, max_points=budget, phase=phase)
                    seen.update(pixel_ids(colors))
                self.assertEqual(seen, set(range(count)))

    def test_invalid_depth_and_low_confidence_pixels_do_not_reenter_when_filling_budget(self):
        sample = frame()
        sample.confidence[::2, ::3] = 0
        sample.depth[20:25, :30] = np.nan
        native, rgb = measured_points(sample)
        ids = pixel_ids(rgb)
        lookup = {int(pixel): point for pixel, point in zip(ids, native)}
        for phase in range(3):
            xyz, colors = measured_points(sample, max_points=1379, phase=phase)
            selected = pixel_ids(colors)
            self.assertEqual(len(xyz), 1379)
            self.assertEqual(len(np.unique(selected)), 1379)
            self.assertTrue(set(selected).issubset(lookup))
            np.testing.assert_array_equal(xyz, np.asarray([lookup[int(pixel)] for pixel in selected]))

    def test_under_budget_frame_keeps_all_native_samples(self):
        sample = frame(6, 8)
        native, rgb = measured_points(sample)
        for budget in (len(native), len(native) + 1, None):
            xyz, colors = measured_points(sample, max_points=budget, phase=100)
            np.testing.assert_array_equal(xyz, native)
            np.testing.assert_array_equal(colors, rgb)


if __name__ == '__main__':
    unittest.main()
