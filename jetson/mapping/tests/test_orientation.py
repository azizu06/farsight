import math
import time
import unittest

import cv2
import numpy as np
from fastapi.testclient import TestClient

from jetson.mapping.demo import simulated_frame
from jetson.mapping.orientation import CONFIRM_FRAMES, FLIP, UprightNormalizer, rotate_frame_180, up_cosine
from jetson.mapping.phone import decode_frame, reconstruct
from jetson.mapping.people import torso_point
from jetson.mapping.server import create_app
from jetson.mapping.tests.test_mapping import bundle

IDENTITY = ("simulated-room", 1)


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
    bgr = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
    _, jpeg = cv2.imencode(".jpg", cv2.rotate(bgr, cv2.ROTATE_180))
    pose = frame.transform @ FLIP
    header = dict(frame.header, transform=pose.flatten(order="F").tolist())
    flipped = type(frame)(header, frame.rgb[::-1, ::-1].copy(), frame.depth[::-1, ::-1].copy(),
                          frame.confidence[::-1, ::-1].copy(),
                          type(k)(k.width, k.height, k.fx, k.fy, w - 1 - k.cx, h - 1 - k.cy),
                          pose, jpeg.tobytes())
    return flipped


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


class UprightFrameTests(unittest.TestCase):
    def setUp(self):
        self.upright = simulated_frame(1.0, 4)
        self.assertGreater(up_cosine(self.upright.transform)[0], 0.9)

    def decode(self, frame, mount):
        return decode_frame(bundle(frame), IDENTITY, mount)

    def test_inverted_phone_gives_the_same_picture_and_world_points_as_upright(self):
        mount = UprightNormalizer()
        flipped = phone_upside_down(self.upright)
        for _ in range(CONFIRM_FRAMES - 1):
            self.assertIs(self.decode(flipped, mount).header.get("orientation_normalized"), None)
        fixed = self.decode(flipped, mount)
        self.assertEqual(fixed.header["orientation_normalized"], "rotated_180")
        baseline = decode_frame(bundle(self.upright), IDENTITY)
        np.testing.assert_allclose(fixed.depth, baseline.depth)
        np.testing.assert_array_equal(fixed.confidence, baseline.confidence)
        np.testing.assert_allclose(fixed.transform, baseline.transform, atol=1e-9)
        self.assertEqual(fixed.calibration, baseline.calibration)
        self.assertLess(np.abs(fixed.rgb.astype(int) - baseline.rgb.astype(int)).mean(), 6)
        jpeg = cv2.imdecode(np.frombuffer(fixed.jpeg, np.uint8), cv2.IMREAD_COLOR)
        reference = cv2.imdecode(np.frombuffer(baseline.jpeg, np.uint8), cv2.IMREAD_COLOR)
        self.assertLess(np.abs(jpeg.astype(int) - reference.astype(int)).mean(), 6)
        # Fusion input is unchanged: the same world points, so the map is not double corrected.
        np.testing.assert_allclose(reconstruct(fixed, stride=1)[0], reconstruct(baseline, stride=1)[0], atol=1e-4)

    def test_person_marker_depth_lookup_matches_after_flip(self):
        mount = UprightNormalizer()
        for _ in range(CONFIRM_FRAMES):
            fixed = self.decode(phone_upside_down(self.upright), mount)
        baseline = decode_frame(bundle(self.upright), IDENTITY)
        box = dict(x=0.35, y=0.3, w=0.2, h=0.4)
        np.testing.assert_allclose(torso_point(box, fixed), torso_point(box, baseline), atol=2e-3)

    def test_upright_phone_is_untouched_and_unmodified_jpeg(self):
        mount = UprightNormalizer()
        frame = self.decode(self.upright, mount)
        self.assertNotIn("orientation_normalized", frame.header)
        self.assertEqual(frame.jpeg, self.upright.jpeg)

    def test_rotation_is_an_involution(self):
        twice = rotate_frame_180(rotate_frame_180(self.upright))
        np.testing.assert_allclose(twice.depth, self.upright.depth)
        np.testing.assert_allclose(twice.transform, self.upright.transform, atol=1e-12)
        self.assertEqual(twice.calibration, self.upright.calibration)


class OrientationServerTests(unittest.TestCase):
    def test_camera_view_and_map_follow_a_live_flip_without_a_session_restart(self):
        upright = simulated_frame(1.0, 4)
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
                        while app.state.mapping.frame_id != sent and time.monotonic() < deadline:
                            time.sleep(.01)

                def camera():
                    return cv2.imdecode(np.frombuffer(client.get("/camera.jpg").content, np.uint8), cv2.IMREAD_COLOR)

                reference = cv2.imdecode(np.frombuffer(upright.jpeg, np.uint8), cv2.IMREAD_COLOR)
                push(upright, 2)
                self.assertLess(np.abs(camera().astype(int) - reference.astype(int)).mean(), 6)
                flipped = phone_upside_down(upright)
                push(flipped, CONFIRM_FRAMES - 1)  # still confirming: the raw sensor picture
                self.assertGreater(np.abs(camera().astype(int) - reference.astype(int)).mean(), 15)
                push(flipped, 1)
                self.assertLess(np.abs(camera().astype(int) - reference.astype(int)).mean(), 6)
                push(upright, CONFIRM_FRAMES)  # flipped back
                self.assertLess(np.abs(camera().astype(int) - reference.astype(int)).mean(), 6)
                self.assertEqual(app.state.mapping.rejected, 0)


if __name__ == "__main__":
    unittest.main()
