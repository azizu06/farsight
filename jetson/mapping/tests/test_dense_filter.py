import unittest

import numpy as np

from jetson.mapping.geometry import Calibration
from jetson.mapping.phone import RGBDFrame, facing_mask, measured_points

W, H = 256, 192
CALIBRATION = Calibration(W, H, 212.0, 212.0, 128.0, 96.0)


def frame(depth, transform=None):
    return RGBDFrame({}, np.full((H, W, 3), 128, np.uint8), depth.astype(np.float32),
                     np.full((H, W), 2, np.uint8), CALIBRATION,
                     np.eye(4) if transform is None else transform, b"")


def plane_depth(normal, point):
    """Optical-axis depth of the plane n.(x - p) = 0 seen by the camera at the origin."""
    v, u = np.mgrid[:H, :W]
    ray = np.stack(((u - CALIBRATION.cx) / CALIBRATION.fx, -(v - CALIBRATION.cy) / CALIBRATION.fy,
                    -np.ones((H, W))), axis=-1)
    return (np.dot(normal, point) / (ray @ np.asarray(normal, float))).astype(np.float32)


def tilted(degrees, axis):
    """Unit normal facing the camera (+z), tilted by `degrees` about x or y."""
    a = np.radians(degrees)
    return np.array([np.sin(a), 0, np.cos(a)]) if axis == "y" else np.array([0, np.sin(a), np.cos(a)])


class DenseFilterTests(unittest.TestCase):
    def test_flat_wall_keeps_every_pixel_including_the_border(self):
        mask = facing_mask(frame(np.full((H, W), 2.0)))
        self.assertTrue(mask.all())

    def test_flying_pixel_ramp_between_foreground_and_background_is_dropped(self):
        depth = np.full((H, W), 3.5, np.float32)
        depth[:, :120] = 1.5
        depth[:, 120:128] = np.linspace(1.5, 3.5, 10)[1:-1]   # smoothed 8-pixel edge ramp
        xyz, _ = measured_points(frame(depth))
        # No surviving sample lies between the two real surfaces.
        self.assertFalse(((-xyz[:, 2] > 1.6) & (-xyz[:, 2] < 3.4)).any())
        self.assertGreater(len(xyz), 0.9 * H * W)

    def test_wall_seen_almost_edge_on_is_dropped_but_a_shallow_floor_is_kept(self):
        # Vertical wall at 84 degrees to the central ray: a flying-pixel orientation.
        # Only pixels near that ray see the full angle, so test the central strip.
        wall = plane_depth(tilted(84, "y"), [0, 0, -2])
        self.assertLess(facing_mask(frame(wall))[60:130, 124:132].mean(), 0.05)
        # The same angle on the floor is kept (ARKit world y is up; camera looks along -z).
        floor = plane_depth(tilted(84, "x"), [0, 0, -2])
        self.assertGreater(facing_mask(frame(floor))[92:100, 60:200].mean(), 0.95)

    def test_moderately_slanted_wall_is_kept(self):
        wall = plane_depth(tilted(60, "y"), [0, 0, -2])
        self.assertGreater(facing_mask(frame(wall))[60:130, 60:200].mean(), 0.95)


if __name__ == "__main__":
    unittest.main()
