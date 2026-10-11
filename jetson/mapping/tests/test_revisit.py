import base64
import json
import unittest

import numpy as np

from jetson.mapping.geometry import Calibration
from jetson.mapping.phone import RGBDFrame, measured_points
from jetson.mapping.revisit import DepthView, POINT_BATCH


def frame(depth=4., width=256, height=192, transform=None):
    depth = np.full((height, width), depth, np.float32) if np.isscalar(depth) else np.asarray(depth, np.float32)
    height, width = depth.shape
    return RGBDFrame({"_mapping_sensor_key": ("phone", 3, 9, 42, 12.5)},
                     np.full((height, width, 3), 128, np.uint8), depth,
                     np.full((height, width), 2, np.uint8),
                     Calibration(width, height, .83 * width, .83 * width, width / 2, height / 2),
                     np.eye(4) if transform is None else transform, b"")


def points_at(sample, uv, depth):
    uv = np.asarray(uv, np.float64).reshape(-1, 2)
    depth = np.broadcast_to(np.asarray(depth, np.float64), (len(uv),))
    k = sample.calibration
    local = np.column_stack(((uv[:, 0] - k.cx) * depth / k.fx,
                             -(uv[:, 1] - k.cy) * depth / k.fy, -depth))
    return local @ sample.transform[:3, :3].T + sample.transform[:3, 3]


class RevisitTests(unittest.TestCase):
    def test_removed_foreground_refreshes_only_observed_region(self):
        sample = frame()
        view = DepthView.from_frame(sample)
        points = points_at(sample, [(128, 96)] * 4, [2., 4., 4.2, 5.])
        np.testing.assert_array_equal(view.contradicted(points), [True, False, False, False])
        np.testing.assert_array_equal(view.observed(points), [True, True, False, False])

    def test_removed_six_cm_object_clears_but_four_cm_behind_surface_is_occluded(self):
        sample = frame(2.)
        view = DepthView.from_frame(sample)
        points = points_at(sample, [(128, 96)] * 3, [1.94, 2., 2.04])
        np.testing.assert_array_equal(view.contradicted(points), [True, False, False])
        np.testing.assert_array_equal(view.observed(points), [True, True, False])

    def test_moved_foreground_clears_old_location_and_preserves_occluded_background(self):
        depths = np.full((192, 256), 4., np.float32)
        depths[60:132, 144:204] = 1.5
        sample = frame(depths)
        view = DepthView.from_frame(sample)
        points = points_at(sample, [(80, 96), (174, 96), (174, 96), (174, 96)],
                           [1.5, 1.5, 4., 1.])
        np.testing.assert_array_equal(view.contradicted(points), [True, False, False, True])
        np.testing.assert_array_equal(view.observed(points), [True, True, False, True])

    def test_still_present_plane_matches_native_pixel_projection(self):
        sample = frame()
        points, _ = measured_points(sample)
        view = DepthView.from_frame(sample)
        self.assertTrue(view.observed(points).all())
        self.assertFalse(view.contradicted(points).any())

    def test_out_of_view_behind_camera_and_nonfinite_points_preserve_geometry(self):
        sample = frame()
        view = DepthView.from_frame(sample)
        points = np.vstack((points_at(sample, [(-1, 96), (256, 96), (128, -1), (128, 192)], 2.),
                            [[0., 0., 2.], [np.nan, 0., -2.], [0., 0., 0.]]))
        self.assertFalse(view.contradicted(points).any())
        self.assertFalse(view.observed(points).any())
        self.assertEqual(view.contradicted(np.empty((0, 3))).shape, (0,))

    def test_unknown_low_confidence_and_out_of_range_native_blocks_are_preserved(self):
        for value, confidence in [(np.nan, 2), (0., 2), (.1, 2), (8.1, 2), (4., 0)]:
            with self.subTest(value=value, confidence=confidence):
                sample = frame()
                sample.depth[96, 128] = value
                sample.confidence[96, 128] = confidence
                view = DepthView.from_frame(sample)
                points = points_at(sample, [(129, 97), (80, 96)], 1.)
                np.testing.assert_array_equal(view.contradicted(points), [False, True])
                np.testing.assert_array_equal(view.observed(points), [False, True])

    def test_grazing_surface_uses_the_existing_measured_point_acceptance(self):
        sample = frame()
        v, u = np.mgrid[:192, :256]
        rays = np.stack(((u - sample.calibration.cx) / sample.calibration.fx,
                         -(v - sample.calibration.cy) / sample.calibration.fy,
                         -np.ones_like(u)), axis=-1)
        angle = np.radians(84)
        normal = np.array([np.sin(angle), 0., np.cos(angle)])
        sample.depth[:] = np.dot(normal, [0., 0., -2.]) / (rays @ normal)
        view = DepthView.from_frame(sample)
        points = points_at(sample, [(128, 96)], 1.)
        self.assertFalse(view.contradicted(points)[0])
        self.assertFalse(view.observed(points)[0])

    def test_rotated_translated_camera_and_exact_native_intrinsics(self):
        angle = np.radians(67)
        transform = np.array([[np.cos(angle), 0., np.sin(angle), 3.],
                              [0., 1., 0., 1.2],
                              [-np.sin(angle), 0., np.cos(angle), -5.],
                              [0., 0., 0., 1.]])
        sample = frame(transform=transform)
        sample.calibration = Calibration(256, 192, 211.75, 204.25, 127.2, 94.7)
        view = DepthView.from_frame(sample)
        points = points_at(sample, [(20, 30), (128, 96), (255, 191)], [2., 4., 5.])
        np.testing.assert_array_equal(view.contradicted(points), [True, False, False])
        np.testing.assert_array_equal(view.observed(points), [True, True, False])
        restored = DepthView.from_metadata(view.to_metadata())
        self.assertEqual(restored.calibration, sample.calibration)
        np.testing.assert_array_equal(restored.contradicted(points), view.contradicted(points))

    def test_thin_foreground_and_partial_native_blocks_are_conservative(self):
        sample = frame(width=259, height=195)
        sample.depth[96, 129] = 1.5  # A single foreground pixel must not be averaged away.
        view = DepthView.from_frame(sample)
        self.assertEqual(view.stride, 3)
        point = points_at(sample, [(130, 97)], 2.)
        self.assertFalse(view.contradicted(point)[0])
        # Padding outside the native image does not make its last block unknown.
        last = points_at(sample, [(258, 194)], 2.)
        self.assertTrue(view.contradicted(last)[0])
        self.assertGreater(view.depth_mm[-1, -1], 0)

    def test_accepted_small_foreground_is_min_pooled_and_mm_quantized_down(self):
        sample = frame()
        sample.depth[96, 128] = 3.9417
        view = DepthView.from_frame(sample)
        self.assertEqual(view.depth_mm[48, 64], 3941)
        # A far background in the same block would contradict this old point,
        # but its thin nearer foreground prevents that replacement.
        point = points_at(sample, [(129, 97)], 3.93)
        self.assertFalse(view.contradicted(point)[0])
        self.assertTrue(DepthView.from_frame(frame()).contradicted(point)[0])

    def test_compact_codec_roundtrip_header_budget_and_capture_identity(self):
        sample = frame(3.1419, width=640, height=480)
        sample.confidence[10, 10] = 0
        view = DepthView.from_frame(sample)
        metadata = json.loads(json.dumps(view.to_metadata()))
        self.assertLess(len(json.dumps({"revisit": metadata}).encode()), 65536)
        self.assertEqual(view.depth_mm.shape, (96, 128))
        self.assertEqual(view.stride, 5)
        self.assertEqual(view.sensor_key, ("phone", 3, 9, 42, 12.5))
        self.assertEqual(len(base64.b64decode(metadata["depth"])), 96 * 128 * 2)
        restored = DepthView.from_metadata(metadata)
        np.testing.assert_array_equal(restored.depth_mm, view.depth_mm)
        np.testing.assert_array_equal(restored.transform, sample.transform)
        self.assertEqual(restored.sensor_key, view.sensor_key)
        points = points_at(sample, [(320, 240), (10, 10), (500, 300)], [2., 2., 4.])
        np.testing.assert_array_equal(restored.contradicted(points), [True, False, False])
        np.testing.assert_array_equal(restored.observed(points), [True, False, False])

    def test_codec_rejects_truncated_or_different_format_without_guessing(self):
        metadata = DepthView.from_frame(frame()).to_metadata()
        for change in [dict(version=2), dict(matrix_order="column_major"),
                       dict(depth=metadata["depth"][:-4]), dict(depth="not base64"),
                       dict(stride=0), dict(width=100000)]:
            with self.subTest(change=list(change)):
                with self.assertRaises(ValueError):
                    DepthView.from_metadata(dict(metadata, **change))

    def test_batched_projection_has_the_same_answer_at_chunk_boundaries(self):
        sample = frame()
        view = DepthView.from_frame(sample)
        depths = np.resize([2., 4., 5.], POINT_BATCH + 3)
        points = points_at(sample, np.tile([128, 96], (len(depths), 1)), depths)
        np.testing.assert_array_equal(view.contradicted(points), depths == 2.)
        np.testing.assert_array_equal(view.observed(points), depths <= 4.)


if __name__ == "__main__":
    unittest.main()
