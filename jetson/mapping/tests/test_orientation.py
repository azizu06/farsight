import math
import concurrent.futures
import threading
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from fastapi.testclient import TestClient

from jetson.camera import CameraFrame, CameraService
from jetson.capture_permits import CapturePermits
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
    def test_rgb_only_source_cannot_reuse_native_lidar_pixels(self):
        held = simulated_frame(1.0, 4)
        locator = PersonLocator()
        locator.offer_frame(held)
        camera = CameraFrame(held.jpeg, 0., 4, 'PHONE RGB')
        box = dict(x=.31, y=.22, w=.2, h=.4, cls='person', id=1)
        self.assertNotIn('world', locator.annotate([box], camera)[0])

    def test_mirrored_camera_with_another_picture_cannot_use_held_depth(self):
        held = simulated_frame(1.0, 4)
        locator = PersonLocator()
        locator.offer_frame(held)
        camera = CameraFrame(b'new socket picture', 0., 4, 'IPHONE LIDAR', mirrored=True)
        box = dict(x=.31, y=.22, w=.2, h=.4, cls='person', id=1)
        self.assertNotIn('world', locator.annotate([box], camera)[0])

    def test_repeated_native_pixels_from_another_socket_cannot_use_held_depth(self):
        held = simulated_frame(1.0, 4)
        held.header['_mapping_sensor_key'] = (*IDENTITY, 1, 4, 1.)
        locator = PersonLocator()
        locator.offer_frame(held)
        camera = CameraFrame(held.jpeg, 0., 4, 'IPHONE LIDAR', mirrored=True,
                             native_jpeg=held.jpeg, sensor_key=(*IDENTITY, 2, 4, 1.))
        box = dict(x=.31, y=.22, w=.2, h=.4, cls='person', id=1)
        self.assertNotIn('world', locator.annotate([box], camera)[0])

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
                             mirrored=True, native_jpeg=flipped.jpeg)
        box = dict(x=0.31, y=0.22, w=0.2, h=0.4, cls="person", id=1)
        expected = torso_point(box, decode_frame(bundle(upright), IDENTITY))
        np.testing.assert_allclose(locator.annotate([dict(box)], camera)[0]["world"], expected, atol=2e-3)


class CameraMountBindingTests(unittest.TestCase):
    def test_late_old_decoder_cannot_advance_the_reconnected_mount(self):
        camera = CameraService(None)
        old, current = CapturePermits(*IDENTITY), CapturePermits(*IDENTITY)
        camera.begin_phone(old)
        inverted = phone_upside_down(simulated_frame(1., 4))
        entered, release = threading.Event(), threading.Event()

        def delayed_decode(*args, **kwargs):
            result = decode_frame(*args, **kwargs)
            entered.set()
            release.wait(2)
            return result

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                with patch('jetson.mapping.phone.decode_frame', delayed_decode):
                    pending = pool.submit(camera.accept_phone_rgbd, bundle(inverted), IDENTITY, -1, None, old)
                    self.assertTrue(entered.wait(1))
                    camera.begin_phone(current)
                    release.set()
                    self.assertEqual(pending.result(timeout=1), -1)
                for number in (5, 6):
                    inverted.header.update(frame_id=number, t_capture=float(number))
                    camera.accept_phone_rgbd(bundle(inverted), IDENTITY, -1, None, current)
                self.assertFalse(camera.mount.inverted, 'An old socket counted toward the new mount confirmation')
                inverted.header.update(frame_id=7, t_capture=7.)
                camera.accept_phone_rgbd(bundle(inverted), IDENTITY, -1, None, current)
                self.assertTrue(camera.mount.inverted)
        finally:
            release.set()
            old.close()
            current.close()


class OrientationServerTests(unittest.TestCase):
    def test_camera_and_map_share_the_receiver_bound_native_capture(self):
        inverted = phone_upside_down(simulated_frame(1., 4))
        app = create_app(detector='none', fusion_bypass=True)
        accepted = {}
        app.state.mapping.frame_observers.append(lambda frame: accepted.update({frame.header['frame_id']: frame}))
        with TestClient(app) as client:
            with client.websocket_connect('/phone') as phone:
                phone.send_json(dict(version=1, type='hello', session_id=IDENTITY[0], map_epoch=IDENTITY[1],
                                     supports_scene_depth=True))
                for number in range(1, CONFIRM_FRAMES + 1):
                    header = dict(inverted.header, frame_id=number, t_capture=float(number),
                                  _mapping_sensor_key=['producer', 99, 99, 99, 99.])
                    value = type(inverted)(header, inverted.rgb, inverted.depth, inverted.confidence,
                                           inverted.calibration, inverted.transform, inverted.jpeg)
                    phone.send_bytes(bundle(value))
                    deadline = time.monotonic() + 1
                    while (number not in accepted or app.state.camera.phone_rgb is None
                           or app.state.camera.phone_rgb.frame_id != number) and time.monotonic() < deadline:
                        time.sleep(.005)
                    self.assertIn(number, accepted)
                camera = app.state.camera.snapshot()
                key = (*IDENTITY, app.state.mapping.socket_generation, CONFIRM_FRAMES, float(CONFIRM_FRAMES))
                self.assertEqual(camera.sensor_key, key)
                self.assertEqual(accepted[CONFIRM_FRAMES].header['_mapping_sensor_key'], key)
                self.assertTrue(camera.mirrored)
                self.assertEqual(camera.native_jpeg, inverted.jpeg)
                preview = app.state.camera.preview(max_width=160)
                self.assertEqual(preview.sensor_key, key)
                self.assertEqual(preview.native_jpeg, inverted.jpeg)
                locator = PersonLocator()
                locator.offer_frame(accepted[CONFIRM_FRAMES])
                box = dict(x=.31, y=.22, w=.2, h=.4, cls='person', id=1)
                expected = torso_point(box, upright_view(accepted[CONFIRM_FRAMES]))
                self.assertEqual(locator.annotate([box], camera)[0].get('world'), expected)

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
