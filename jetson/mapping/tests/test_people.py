import unittest

import numpy as np

from jetson.camera import CameraFrame
from jetson.mapping.people import HELD_FRAMES, PersonLocator, torso_point
from jetson.mapping.tests.test_mapping import flat_frame


def person(**changes):
    return dict(dict(cls="person", id=3, conf=.9, x=.25, y=0, w=.5, h=1), **changes)


def camera(frame):
    return CameraFrame(frame.jpeg, 0.0, frame.header["frame_id"], "IPHONE LIDAR")


class PersonLocatorTests(unittest.TestCase):
    def test_torso_point_uses_measured_depth_and_pose(self):
        frame = flat_frame()
        frame.transform[:3, 3] = [1, 0, 0]
        np.testing.assert_allclose(torso_point(person(), frame), [1, 0.125, -2])
        frame.confidence[:] = 0
        self.assertIsNone(torso_point(person(), frame))

    def test_only_tracked_people_on_the_held_rgbd_frame_get_world(self):
        locator, frame = PersonLocator(), flat_frame()
        locator.offer_frame(frame)
        boxes = locator.annotate([person(), person(id=None), person(cls="chair")], camera(frame))
        self.assertEqual(boxes[0]["world"], [0.0, 0.125, -2.0])
        self.assertNotIn("world", boxes[1])
        self.assertNotIn("world", boxes[2])
        # Same frame id but another image (RGB-only source): no 3D position.
        other = CameraFrame(b"rgb-only", 0.0, frame.header["frame_id"], "IPHONE")
        self.assertNotIn("world", locator.annotate([person()], other)[0])

    def test_limited_tracking_and_old_frames_are_not_used(self):
        locator, limited = PersonLocator(), flat_frame()
        limited.header["tracking"] = "limited"
        locator.offer_frame(limited)
        self.assertNotIn("world", locator.annotate([person()], camera(limited))[0])
        first = flat_frame()
        locator.offer_frame(first)
        for i in range(1, HELD_FRAMES + 1):
            later = flat_frame()
            later.header["frame_id"] = i
            locator.offer_frame(later)
        self.assertNotIn("world", locator.annotate([person()], camera(first))[0])
