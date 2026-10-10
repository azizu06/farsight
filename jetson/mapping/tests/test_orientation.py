import math
import time
import unittest

import cv2
import numpy as np
from fastapi.testclient import TestClient

from jetson.camera import CameraFrame
from jetson.mapping.demo import simulated_frame
from jetson.mapping.orientation import CONFIRM_FRAMES, UprightNormalizer, rotate_jpeg_180, up_cosine, upright_view
from jetson.mapping.people import PersonLocator, torso_point
from jetson.mapping.phone import decode_frame
from jetson.mapping.server import create_app
from jetson.mapping.tests.test_mapping import bundle

IDENTITY = ("simulated-room", 1)
FLIP = np.diag([-1.0, -1.0, 1.0, 1.0])


def roll_pose(degrees, pitch=0.0):
    """Camera-to-world pose rolled about the optical axis; pitch tilts toward the floor."""
    r, p = math.radians(degrees), math.radians(pitch)
    roll = np.array([[math.cos(r), -math.sin(r), 0], [math.sin(r), math.cos(r), 0], [0, 0, 1]])
    tilt = np.array([[1, 0, 0], [0, math.cos(p), -math.sin(p)], [0, math.sin(p), math.cos(p)]])
    pose = np.eye(4)
    pose[:3, :3] = tilt @ roll
    pose[:3, 3] = [0.4, 0.3, -0.2]
    return pose


def phone_upside_down(frame):
    """What a phone rotated 180 degrees reports for the same scene: sensor-native flip."""
    h, w = frame.depth.shape
    k = frame.calibration
    pose = frame.transform @ FLIP
    header = dict(frame.header, transform=pose.flatten(order="F").tolist())
    return type(frame)(header, frame.rgb[::-1, ::-1].copy(), frame.depth[::-1, ::-1].copy(),
                       frame.confidence[::-1, ::-1].copy(),
                       type(k)(k.width, k.height, k.fx, k.fy, w - 1 - k.cx, h - 1 - k.cy),
                       pose, rotate_jpeg_180(frame.jpeg))


def gap(a, b):
    return np.abs(a.astype(int) - b.astype(int)).mean()


def decoded(jpeg):
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


class OrientationDetectionTests(unittest.TestCase):
    def test_gravity_cosine_is_plus_one_upright_and_minus_one_inverted(self):
        self.assertAlmostEqual(up_cosine(roll_pose(0))[0], 1)
        self.assertAlmostEqual(up_cosine(roll_pose(180))[0], -1)
        self.assertAlmostEqual(up_cosine(roll_pose(180, pitch=20))[0], -1)

    def test_flips_after_confirmation_and_back_with_hysteresis(self):
        mount = UprightNormalizer()
        for _ in range(CONFIRM_FRAMES + 2):
            self.assertFalse(mount.observe(roll_pose(0)))
        for _ in range(10):  # near portrait: inside the dead band, never flips
            self.assertFalse(mount.observe(roll_pose(95)))
        for _ in range(CONFIRM_FRAMES - 1):
            self.assertFalse(mount.observe(roll_pose(170)))
        self.assertFalse(mount.observe(roll_pose(0)))  # one good frame resets the count
        for _ in range(CONFIRM_FRAMES - 1):
            self.assertFalse(mount.observe(roll_pose(170)))
        self.assertTrue(mount.observe(roll_pose(170)))
        for _ in range(10):
            self.assertTrue(mount.observe(roll_pose(100)))  # wobble stays inverted
        for _ in range(CONFIRM_FRAMES - 1):
            self.assertTrue(mount.observe(roll_pose(5)))
        self.assertFalse(mount.observe(roll_pose(5)))

    def test_looking_straight_down_or_up_holds_the_current_mount(self):
        mount = UprightNormalizer()
        for _ in range(CONFIRM_FRAMES):
            mount.observe(roll_pose(180))
        self.assertTrue(mount.inverted)
        for _ in range(10):
            self.assertTrue(mount.observe(roll_pose(0, pitch=85)))  # roll undefined near vertical


class PersonMarkerTests(unittest.TestCase):
    def test_box_on_the_upright_picture_finds_the_same_depth_and_world_point(self):
        upright = simulated_frame(1.0, 4)
        sensor = decode_frame(bundle(phone_upside_down(upright)), IDENTITY)  # sensor-native, no pose edit
        baseline = decode_frame(bundle(upright), IDENTITY)
        box = dict(x=0.31, y=0.22, w=0.2, h=0.4, cls="person", id=1)
        # The box is drawn on the upright picture; the flipped sensor is viewed rotated.
        np.testing.assert_allclose(torso_point(box, upright_view(sensor)), torso_point(box, baseline), atol=2e-3)
        np.testing.assert_allclose(upright_view(sensor).depth, baseline.depth)
        np.testing.assert_allclose(upright_view(sensor).transform, baseline.transform, atol=1e-9)
        self.assertEqual(upright_view(sensor).calibration, baseline.calibration)

    def test_locator_annotates_through_the_mirrored_camera_frame(self):
        upright = simulated_frame(1.0, 4)
        flipped = decode_frame(bundle(phone_upside_down(upright)), IDENTITY)
        locator = PersonLocator()
        locator.offer_frame(flipped)
        camera = CameraFrame(rotate_jpeg_180(flipped.jpeg), 0.0, flipped.header["frame_id"], "IPHONE LIDAR",
                             mirrored=True)
        box = dict(x=0.31, y=0.22, w=0.2, h=0.4, cls="person", id=1)
        expected = torso_point(box, decode_frame(bundle(upright), IDENTITY))
        np.testing.assert_allclose(locator.annotate([dict(box)], camera)[0]["world"], expected, atol=2e-3)


class OrientationServerTests(unittest.TestCase):
    def test_camera_view_follows_a_live_flip_while_the_map_is_untouched(self):
        upright = simulated_frame(1.0, 4)
        flipped = phone_upside_down(upright)
        app = create_app()
        with TestClient(app) as client:
            with client.websocket_connect("/phone") as phone:
                phone.send_json(dict(version=1, type="hello", session_id=IDENTITY[0], map_epoch=1,
                                     device="Test", supports_scene_depth=True))
                sent = 0

                def push(frame, count):
                    nonlocal sent
                    for _ in range(count):
                        sent += 1
                        header = dict(frame.header, frame_id=sent, t_capture=float(sent))
                        phone.send_bytes(bundle(type(frame)(header, frame.rgb, frame.depth, frame.confidence,
                                                            frame.calibration, frame.transform, frame.jpeg)))
                        deadline = time.monotonic() + 3
                        while ((app.state.camera.phone_rgb is None or app.state.camera.phone_rgb.frame_id != sent)
                               and time.monotonic() < deadline):
                            time.sleep(.01)

                def camera():
                    return decoded(client.get("/camera.jpg").content)

                reference = decoded(upright.jpeg)
                push(upright, 2)
                self.assertLess(gap(camera(), reference), 6)
                push(flipped, CONFIRM_FRAMES - 1)  # still confirming: the raw upside-down sensor picture
                self.assertGreater(gap(camera(), reference), 15)
                push(flipped, 1)
                self.assertLess(gap(camera(), reference), 6)
                self.assertTrue(app.state.camera.phone_rgb.mirrored)
                push(upright, CONFIRM_FRAMES)  # phone turned back
                self.assertLess(gap(camera(), reference), 6)
                self.assertFalse(app.state.camera.phone_rgb.mirrored)
                self.assertEqual(app.state.mapping.rejected, 0)


if __name__ == "__main__":
    unittest.main()
