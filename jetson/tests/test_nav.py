"""Server side of the Nav2 seam (jetson/nav.py): contract, lease gating, bridge protocol."""

import asyncio
import math
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from jetson.control.state import ControlState
from jetson.camera import CameraService
from jetson.mapping.demo import simulated_frame
from jetson.mapping.phone import decode_frame
from jetson.mapping.tests.test_mapping import bundle, flat_frame
from jetson.mapping.server import create_app
from jetson.mapping.state import MapState
from jetson.nav import NavLane, NavService, Refused, decode_depth, encode_depth, odom_to_world, simplify, world_to_odom
from jetson.perception import PerceptionService

NAV_KEYS = {"state", "mode", "goal", "path", "frontiers", "backend", "message", "revision"}
PHONE = ("phone-session", 1)


def feed(nav, tracking="normal", identity=PHONE, frame=True):
    """A fresh ARKit pose (and, with ``frame``, a LiDAR depth frame) of one sensor session."""
    mapping = nav.mapping
    with mapping.lock:
        mapping.identity, mapping.connected, mapping.tracking = identity, True, tracking
        mapping.last_received, mapping.pose = time.monotonic(), np.eye(4)
    if frame:
        from jetson.mapping.geometry import Calibration
        depth = np.full((120, 160), 3.0, np.float32)
        nav.offer_frame(type("Frame", (), dict(
            header=dict(session_id=identity[0], map_epoch=identity[1], tracking=tracking,
                        _received_monotonic=time.monotonic(),
                        _mapping_socket_generation=getattr(mapping, "socket_generation", 0),
                        frame_id=nav.frame_seq + 1, t_capture=float(nav.frame_seq + 1),
                        _mapping_sensor_key=(*identity, getattr(mapping, "socket_generation", 0),
                                             nav.frame_seq + 1, float(nav.frame_seq + 1))),
            depth=depth, confidence=np.full(depth.shape, 2, np.uint8), transform=np.eye(4),
            calibration=Calibration(160, 120, 135, 135, 79.5, 59.5)))())
        return nav.frame


def detected(sensor_frame, *, mirrored=False, **changes):
    value = dict(boxes=[dict(id=4, cls="person", x=.45, y=.2, w=.1, h=.6)],
                 fresh=True, error=None, source="IPHONE LIDAR", mirrored=mirrored,
                 frame=sensor_frame.header["frame_id"],
                 sensor_key=list(sensor_frame.header["_mapping_sensor_key"]))
    value.update(changes)
    return value


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class NavSocket:
    """Loopback bridge fixture; stop after one complete sender iteration."""
    client = SimpleNamespace(host="127.0.0.1")

    def __init__(self, on_json=None):
        self.done = asyncio.Event()
        self.records = []
        self.on_json = on_json

    async def accept(self):
        pass

    async def close(self, **kwargs):
        self.done.set()

    async def receive_json(self):
        return dict(type="hello", version=1)

    async def receive(self):
        await self.done.wait()
        return dict(type="websocket.disconnect")

    async def send_json(self, message):
        self.records.append(message)
        if self.on_json:
            self.on_json(message)
        if message["type"] == "intent":
            self.done.set()

    async def send_bytes(self, packet):
        self.records.append(decode_depth(packet)[0])


class SenderAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = time.monotonic()
        self.nav = NavService(MapState(), clock=lambda: self.now)
        feed(self.nav)
        self.nav.frame_at = self.now
        self.nav.mapping.last_received = self.now

    async def run_sender(self, socket):
        with patch("jetson.nav.time", SimpleNamespace(monotonic=lambda: self.now)):
            await asyncio.wait_for(self.nav.websocket(socket), 1)

    async def test_depth_age_includes_encoding_delay_at_publication(self):
        socket = NavSocket()
        def delayed_encoder(frame, **kwargs):
            self.now += .2
            return encode_depth(frame, **kwargs)
        with patch("jetson.nav.encode_depth", delayed_encoder):
            await self.run_sender(socket)
        depth = next(record for record in socket.records if record["type"] == "depth")
        self.assertEqual(depth["age_ms"], 200.0)

    async def test_depth_that_expires_during_encoding_is_not_published(self):
        socket = NavSocket()
        def delayed_encoder(frame, **kwargs):
            self.now += 1.1
            return encode_depth(frame, **kwargs)
        with patch("jetson.nav.encode_depth", delayed_encoder):
            await self.run_sender(socket)
        self.assertFalse(any(record["type"] == "depth" for record in socket.records))

    def reset_world(self):
        self.nav.mapping.start_session(("second", 2), "fixture")
        feed(self.nav, identity=("second", 2))
        transform = np.eye(4)
        transform[0, 3] = 99
        self.nav.mapping.pose = transform
        self.nav.frame.transform = transform
        self.nav.mapping.last_received = self.nav.frame_at = self.now

    def assert_generation_order(self, records):
        announced = None
        for record in records:
            if record["type"] in ("hello", "reset"):
                announced = record["generation"]
            elif record["type"] in ("pose", "depth", "intent"):
                self.assertEqual(record["generation"], announced, record)
        self.assertTrue(any(record["type"] == "reset" for record in records))

    async def test_reset_during_pose_snapshot_never_labels_new_world_as_old_generation(self):
        socket = NavSocket()
        snapshot = self.nav.pose_snapshot
        reset = False
        def resetting_snapshot():
            nonlocal reset
            if not reset:
                reset = True
                self.reset_world()
            return snapshot()
        with patch.object(self.nav, "pose_snapshot", resetting_snapshot):
            await self.run_sender(socket)
        self.assert_generation_order(socket.records)
        for record in socket.records:
            if record["type"] == "pose" and record["transform"][12] == 99:
                self.assertEqual(record["generation"], self.nav.generation)

    async def test_reset_after_pose_is_announced_before_new_depth_and_intent(self):
        reset = False
        def reset_after_pose(message):
            nonlocal reset
            if message["type"] == "pose" and not reset:
                reset = True
                self.reset_world()
        socket = NavSocket(on_json=reset_after_pose)
        await self.run_sender(socket)
        self.assert_generation_order(socket.records)
        self.assertTrue(any(record["type"] == "depth" and record["generation"] == self.nav.generation
                            for record in socket.records))


class PhysicalPendingSeekTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = Clock()
        self.mapping = MapState(fusion_bypass=True)
        self.socket = self.mapping.start_session(PHONE, "fixture")
        self.mapping.paused = True
        self.camera = CameraService(self.mapping)
        self.nav = NavService(self.mapping, clock=self.clock, camera_snapshot=self.camera.snapshot)
        self.mapping.frame_observers.append(self.nav.offer_frame)
        self.perception = PerceptionService(self.camera, mode="none", fps=100)
        self.perception._detect_primary = lambda image: [dict(cls="person", x=.45, y=.2, w=.1, h=.6)]
        self.inferred = {number: asyncio.Event() for number in (1, 2)}
        loop = asyncio.get_running_loop()
        self.perception.on_detection = lambda packet, frame: loop.call_soon_threadsafe(self.inferred[frame.frame_id].set)
        self.state = ControlState(clock=self.clock, navigator=self.nav, detections=self.perception.snapshot,
                                  proximity_stop=False)
        self.nav.attach(self.state)
        self.state.set_connected(True)
        self.session, self.seq = self.state.begin(), 0
        self.gate, self.entered = threading.Event(), threading.Event()
        self.pending = None
        wire, received = self.publish_camera(1, 3.)
        self.consume(wire, received)
        self.perception.task = asyncio.create_task(self.perception._run())
        await asyncio.wait_for(self.inferred[1].wait(), 1)
        self.old_packet = self.perception.snapshot()
        self.nav.bridge = object()
        self.nav.receive(dict(type="status", generation=self.nav.generation, state="idle", goal_id=None))
        self.send(deadman=False)
        self.send()
        self.state.output()
        self.command()
        self.assertGreater(self.state.output()[0], 0)

    async def asyncTearDown(self):
        self.gate.set()
        if self.pending is not None:
            await asyncio.wait_for(self.pending, 1)
        await self.perception.close()
        self.mapping.dense_worker.close()

    def send(self, **values):
        self.seq += 1
        status = self.state.status(issue_permit=True)
        message = dict(session_id=self.session, permit=status["permit"], seq=self.seq, deadman=True,
                       v=0, w=0, pan=0, mode="seek", target_id=1, video_fresh=True, video_age_ms=0)
        message.update(values)
        self.assertTrue(self.state.accept(message, self.session))

    def command(self):
        self.nav.receive(dict(type="cmd_vel", generation=self.nav.generation, linear=.2, angular=0,
                              goal_id=self.nav.intent()["goal_id"]))

    def publish_camera(self, number, distance):
        frame = flat_frame()
        frame.depth[:] = distance
        frame.header.update(session_id=PHONE[0], map_epoch=PHONE[1], frame_id=number, t_capture=float(number))
        wire, received = bundle(frame), time.monotonic()
        self.camera.accept_phone_rgbd(wire, PHONE, received_at=received, socket_generation=self.socket)
        return wire, received

    def consume(self, wire, received):
        frame = decode_frame(wire, PHONE)
        frame.header["_received_monotonic"] = received
        self.mapping.process(frame, socket_generation=self.socket)

    async def pending_capture(self):
        wire, received = self.publish_camera(2, 6.)
        def blocked_consume():
            self.entered.set()
            if not self.gate.wait(1):
                raise AssertionError("fixture map consumer was not released")
            self.consume(wire, received)
        self.pending = asyncio.create_task(asyncio.to_thread(blocked_consume))
        self.assertTrue(await asyncio.to_thread(self.entered.wait, 1))
        await asyncio.wait_for(self.inferred[2].wait(), 1)
        self.assertEqual(self.perception.snapshot()["frame"], 2)
        self.assertEqual(self.nav.frame.header["frame_id"], 1)
        self.assertIsNone(self.nav.sensor_fault())
        return self.perception.snapshot()

    async def test_pending_real_camera_inference_preserves_fresh_paired_goal_without_renewing_it(self):
        goal, cmd, seek = self.nav.intent()["goal_id"], self.nav.cmd, self.nav.seek.copy()
        await self.pending_capture()
        self.old_packet["boxes"][0]["id"] = 99  # publication cannot mutate Nav's retained input
        self.assertGreater(self.state.output()[0], 0)
        self.assertEqual(self.nav.intent()["goal_id"], goal)
        self.assertEqual(self.nav.seek, seek)
        self.assertIs(self.nav.cmd, cmd)
        self.gate.set()
        await asyncio.wait_for(self.pending, 1)
        self.nav.seek_checked = 0
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertNotEqual(self.nav.intent()["goal_id"], goal)
        self.assertEqual(self.nav.seek["person"], (0., -6.))

    async def test_pending_camera_does_not_hide_invalid_or_absent_target_metadata(self):
        packet = await self.pending_capture()
        for change in (dict(source="PHONE RGB"), dict(sensor_key=[*PHONE, self.socket, 99, 99.]),
                       dict(frame=99), dict(mirrored=None), dict(boxes=[])):
            with self.subTest(change=change):
                self.state.detections = lambda: self.old_packet
                self.nav.seek_checked = 0
                self.state.output()
                self.command()
                self.assertGreater(self.state.output()[0], 0)
                bad = dict(packet, **change)
                self.state.detections = lambda: bad
                self.assertEqual(self.state.output()[:2], (0, 0))
                self.assertIsNone(self.nav.intent()["goal"])
                self.assertIsNone(self.nav.cmd)

    async def test_pending_camera_cannot_extend_expired_pair(self):
        await self.pending_capture()
        with patch("jetson.nav.time", SimpleNamespace(monotonic=lambda: self.nav.frame_at + 1.1)):
            self.assertEqual(self.state.output()[:2], (0, 0))
            self.assertIsNone(self.nav.intent()["goal"])

    async def test_pending_camera_cannot_extend_deadman_or_command_lease(self):
        await self.pending_capture()
        self.assertGreater(self.state.output()[0], 0)
        self.clock.now += .3
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertIsNone(self.nav.seek)

    async def test_pending_camera_callback_failure_holds(self):
        await self.pending_capture()
        def unavailable():
            raise RuntimeError("fixture camera unavailable")
        self.nav.camera_snapshot = unavailable
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertIsNone(self.nav.cmd)

    async def test_pending_camera_that_expires_during_proof_cannot_preserve_goal(self):
        await self.pending_capture()
        clock = SimpleNamespace(now=time.monotonic())
        def delayed_snapshot():
            result = self.camera.snapshot()
            clock.now += 1.1
            return result
        self.nav.camera_snapshot = delayed_snapshot
        with patch("jetson.nav.time", SimpleNamespace(monotonic=lambda: clock.now)):
            self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertIsNone(self.nav.intent()["goal"])


class GeometryTests(unittest.TestCase):
    def test_world_odom_round_trip_and_axes(self):
        # One metre ahead of the start pose (-z) is odom +X; one metre right (+x) is odom -Y.
        self.assertEqual(world_to_odom(0, -1), (1, 0))
        self.assertEqual(world_to_odom(1, 0), (0, -1))
        for x, z in ((0.3, -2.0), (-4.5, 1.25)):
            self.assertEqual(odom_to_world(*world_to_odom(x, z)), (x, z))

    def test_simplify_keeps_corners_and_caps_length(self):
        line = [(i * 0.05, 0.0) for i in range(100)] + [(5.0, i * 0.05) for i in range(1, 60)]
        reduced = simplify(line, 0.01, 200)
        self.assertEqual(reduced[0], line[0])
        self.assertEqual(reduced[-1], line[-1])
        self.assertIn((4.95, 0.0), reduced[:3] + reduced[1:3] + [p for p in reduced if p[1] == 0][-1:])
        self.assertLessEqual(len(reduced), 4)
        zigzag = [(i * 0.1, (i % 2) * 0.5) for i in range(1000)]
        self.assertEqual(len(simplify(zigzag, 0.01, 200)), 200)

    def test_depth_packet_round_trip_drops_low_confidence(self):
        frame = simulated_frame(0.5, 7)
        frame.confidence[:10] = 0
        packet = encode_depth(frame, floor_y=-0.15, source="simulated", age_ms=12.5)
        header, depth = decode_depth(packet)
        self.assertEqual((header["width"], header["height"]), (160, 120))
        self.assertEqual(header["frame_id"], 7)
        self.assertAlmostEqual(header["fx"], frame.calibration.fx)
        self.assertTrue(np.all(depth[:10] == 0))
        valid = depth > 0
        self.assertTrue(valid.any())
        self.assertLess(np.abs(depth[valid] - frame.depth[valid]).max(), 0.001)
        np.testing.assert_allclose(np.asarray(header["transform"]).reshape(4, 4, order="F"), frame.transform)

    def test_large_depth_is_subsampled_with_scaled_intrinsics(self):
        frame = simulated_frame(0.5, 1)
        frame.depth = np.repeat(np.repeat(frame.depth, 2, 0), 2, 1)
        frame.confidence = np.repeat(np.repeat(frame.confidence, 2, 0), 2, 1)
        header, depth = decode_depth(encode_depth(frame, floor_y=-0.15, source="arkit", age_ms=0))
        self.assertEqual(depth.shape, (120, 160))
        self.assertAlmostEqual(header["fx"], frame.calibration.fx / 2)


class LeaseTests(unittest.TestCase):
    """ControlState applies Nav2 commands only in an engaged autonomy mode with a live lease."""

    def setUp(self):
        self.clock = Clock()
        self.nav = NavService(MapState(), clock=self.clock)
        self.nav.bridge = object()  # a connected bridge, without a socket
        feed(self.nav)
        self.state = ControlState(clock=self.clock, navigator=self.nav, proximity_stop=False)
        self.nav.attach(self.state)
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.seq = 0
        self.send(deadman=False)

    def send(self, **values):
        self.seq += 1
        status = self.state.status(issue_permit=True)
        message = dict(session_id=self.session, permit=status["permit"], seq=self.seq, deadman=True,
                       v=0, w=0, pan=0, mode="teleop", video_fresh=True, video_age_ms=0)
        message.update(values)
        return self.state.accept(message, self.session)

    def bridge(self, **message):
        message.setdefault("generation", self.nav.generation)
        if message.get("type") == "cmd_vel":
            message.setdefault("goal_id", self.nav.intent()["goal_id"])
        self.nav.receive(message)

    def test_goal_needs_backend_controller_and_a_valid_permit_while_held(self):
        self.nav.bridge = None
        with self.assertRaises(Refused) as missing:
            self.nav.set_goal(1, -2)
        self.assertEqual((missing.exception.nav["state"], missing.exception.nav["backend"]), ("unavailable", None))
        self.nav.bridge = object()
        self.bridge(type="status", state="idle", goal_id=None)
        with self.assertRaises(Refused) as foreign:
            self.nav.set_goal(1, -2, session_id="another-session")
        self.assertIn("session", foreign.exception.message)
        self.assertTrue(self.send())  # held: a stale permit is still refused, and the press keeps going
        with self.assertRaises(Refused) as unheld:
            self.nav.set_goal(1, -2, session_id=self.session, permit="expired")
        self.assertIn("permit", unheld.exception.message)
        self.assertEqual(set(unheld.exception.nav), NAV_KEYS)
        self.assertEqual(self.state.status()["reason"], "active")
        self.state.end(self.session)
        with self.assertRaises(Refused) as nobody:
            self.nav.set_goal(1, -2)
        self.assertIn("controller", nobody.exception.message)
        self.assertIsNone(self.nav.goal)

    def test_goal_stages_while_stopped_and_drives_only_on_a_new_press(self):
        self.bridge(type="status", state="idle", goal_id=None)
        nav = self.nav.set_goal(1.0, -2.0, session_id=self.session)  # deadman released: no lease
        self.assertEqual(set(nav), NAV_KEYS)
        self.assertEqual((nav["mode"], nav["state"], nav["goal"]), (None, "idle", {"x": 1.0, "z": -2.0}))
        self.assertFalse(self.nav.intent()["active"])
        self.bridge(type="cmd_vel", linear=0.3, angular=0, goal_id=self.nav.goal_id)
        self.assertEqual(self.state.output()[:2], (0, 0))  # staged: nothing moves without a lease
        self.assertTrue(self.send(mode="goto"))
        self.assertEqual(self.state.output()[:2], (0, 0))  # the command from before the press is discarded
        self.bridge(type="cmd_vel", linear=0.2, angular=0, goal_id=self.nav.goal_id)
        self.assertGreater(self.state.output()[0], 0)
        self.assertEqual(self.state.status()["nav"]["goal"], {"x": 1.0, "z": -2.0})

    def test_staged_goal_needs_a_release_and_a_drive_ready_camera(self):
        camera = dict(ready=True)
        self.state = ControlState(clock=self.clock, navigator=self.nav, proximity_stop=False,
                                  camera_ready=lambda: camera["ready"])
        self.nav.attach(self.state)
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.send(deadman=False)
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send())
        self.clock.now += 0.3  # trigger still held while the map view sends no heartbeats: the lease expires
        self.nav.set_goal(0.5, -1.0)
        self.assertEqual(self.state.status()["reason"], "lease_expired")
        self.assertFalse(self.send(mode="goto"))  # back in the camera view: no resume without a release
        self.assertEqual(self.state.status()["reason"], "release_required")
        self.send(deadman=False)
        camera["ready"] = False
        self.assertFalse(self.send(mode="goto"))
        self.assertEqual(self.state.status()["reason"], "camera_stale")
        camera["ready"] = True
        self.send(deadman=False)
        self.assertTrue(self.send(mode="goto"))
        self.assertEqual(self.state.output()[:2], (0, 0))  # only commands computed after this press count
        self.bridge(type="cmd_vel", linear=0.2, angular=0, goal_id=self.nav.goal_id)
        self.assertGreater(self.state.output()[0], 0)
        self.assertEqual(self.nav.goal, (0.5, -1.0))

    def test_goto_drives_from_fresh_cmd_vel_and_stops_on_stale_release_or_teleop(self):
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send())
        nav = self.nav.set_goal(1.0, -2.0)
        self.assertEqual(set(nav), NAV_KEYS)
        self.assertEqual((nav["mode"], nav["state"], nav["backend"]), ("goto", "planning", "nav2"))
        self.assertEqual(nav["goal"], {"x": 1.0, "z": -2.0})
        self.assertFalse(self.nav.intent()["active"])  # armed only; the operator's goto heartbeat starts it
        self.assertTrue(self.send(mode="goto"))
        self.assertEqual(self.state.output()[:2], (0, 0))  # no command yet
        intent = self.nav.intent()
        self.assertTrue(intent["active"])
        self.assertEqual(intent["goal"], {"x": 2.0, "y": -1.0, "yaw": None})
        # ROS angular.z is counter-clockwise; negative turns right (left wheel faster).
        self.bridge(type="cmd_vel", linear=0.225, angular=-1.5)
        left, right, _ = self.state.output()
        self.assertEqual((left, right), (100, 0))
        self.bridge(type="status", state="driving", goal_id=intent["goal_id"], path=[[0, 0], [1, 0], [2, -1]])
        status = self.state.status()
        self.assertEqual(status["mode"], "goto")
        self.assertEqual(status["nav"]["state"], "driving")
        self.assertEqual(status["nav"]["path"], [[0.0, 0.0], [0.0, -1.0], [1.0, -2.0]])
        for _ in range(2):  # heartbeats stay fresh while the command ages past one lease
            self.clock.now += 0.15
            self.assertTrue(self.send(mode="goto"))
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.bridge(type="cmd_vel", linear=0.1, angular=0)
        self.assertGreater(self.state.output()[0], 0)
        self.send(deadman=False)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertFalse(self.nav.intent()["active"])
        released = self.state.status()["nav"]
        self.assertEqual((released["state"], released["goal"]), ("idle", {"x": 1.0, "z": -2.0}))  # still armed
        self.assertTrue(self.send(mode="goto"))  # a new press resumes the same goal
        self.state.output()
        self.assertTrue(self.nav.intent()["active"])
        self.assertEqual(self.nav.intent()["goal_id"], intent["goal_id"])
        self.assertTrue(self.send(mode="goto", v=0.3))  # stick input disarms
        self.assertIsNone(self.nav.goal)

    def test_revision_changes_only_with_path_or_frontiers(self):
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send())
        self.nav.set_goal(1.0, -2.0)
        self.assertTrue(self.send(mode="goto"))
        self.state.output()
        before = self.state.status()["nav"]["revision"]
        self.assertEqual(self.state.status()["nav"]["revision"], before)
        goal_id = self.nav.intent()["goal_id"]
        self.bridge(type="status", state="driving", goal_id=goal_id, path=[[0, 0], [1, 0], [2, -1]])
        self.assertEqual(self.state.status()["nav"]["revision"], before + 1)
        self.bridge(type="status", state="driving", goal_id=goal_id, path=[[0, 0], [1, 0], [2, -1]], message="1 m")
        self.assertEqual(self.state.status()["nav"]["revision"], before + 1)  # same arrays, new message
        self.bridge(type="status", state="driving", goal_id=goal_id, path=[[0, 0], [2, -1]])
        self.assertEqual(self.state.status()["nav"]["revision"], before + 2)

    def test_drive_profile_maps_nav2_si_command_straight_to_wheel_pwm(self):
        from jetson.control.tests.test_drive_profile import make_profile
        profile = make_profile()
        self.state = ControlState(clock=self.clock, navigator=self.nav, proximity_stop=False, drive_profile=profile)
        self.nav.attach(self.state)
        hello = self.nav.hello()
        self.assertEqual(hello["camera_yaw_offset_rad"], profile.camera_yaw_offset_rad)  # bridge yaws base_link
        self.assertEqual(hello["drive_profile"]["half_track_m"], profile.half_track_m)
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.send(deadman=False)
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send())
        self.nav.set_goal(1.0, -2.0)
        self.assertTrue(self.send(mode="goto"))
        self.assertEqual(self.state.output()[:2], (0, 0))  # no command yet
        self.bridge(type="cmd_vel", linear=0.3, angular=0.8)  # forward, curving left (CCW)
        left, right, _ = self.state.output()
        self.assertEqual((left, right), profile.wheel_pwm(0.3, 0.8, max_pwm=self.state.max_pwm))
        self.assertGreater(right, left)
        self.bridge(type="cmd_vel", linear=5.0, angular=0.0)  # beyond the calibrated top speed
        top = profile.max_linear_mps(self.state.max_pwm)
        self.assertEqual(self.state.output()[:2], profile.wheel_pwm(top, 0.0, max_pwm=self.state.max_pwm))
        self.clock.now += 0.15
        self.assertTrue(self.send(mode="goto"))
        self.clock.now += 0.15  # command older than CMD_TTL stops even with a held lease
        self.assertEqual(self.state.output()[:2], (0, 0))

    def drive_goto(self):
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send())
        self.nav.set_goal(0.5, -1.0)
        self.assertTrue(self.send(mode="goto"))
        self.state.output()
        self.bridge(type="cmd_vel", linear=0.2, angular=0.0, goal_id=self.nav.goal_id)
        self.assertGreater(self.state.output()[0], 0)

    def test_rgb_camera_alone_never_starts_nav_motion(self):
        # Review 008 (1): fresh camera, permit, bridge and cmd_vel, but no measured depth or ARKit pose.
        self.nav = NavService(MapState(), clock=self.clock)
        self.nav.bridge = object()
        self.state = ControlState(clock=self.clock, navigator=self.nav, proximity_stop=False)
        self.nav.attach(self.state)
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.send(deadman=False)
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send())
        with self.assertRaises(Refused) as refused:
            self.nav.set_goal(0.5, -1.0)
        self.assertIn("pose", refused.exception.message)
        self.assertEqual(self.nav.status_object()["state"], "unavailable")
        feed(self.nav, frame=False)  # pose but still no depth
        with self.assertRaises(Refused) as refused:
            self.nav.set_goal(0.5, -1.0)
        self.assertIn("depth", refused.exception.message)
        feed(self.nav)
        self.nav.set_goal(0.5, -1.0)
        self.nav.frame_at -= NavService.DEPTH_TTL  # depth stops arriving after the goal was accepted
        self.send(mode="goto")  # the press is accepted, then the sensor gate stops it
        self.bridge(type="cmd_vel", linear=0.3, angular=0, goal_id=self.nav.goal_id)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertEqual(self.state.status()["reason"], "nav_depth_lost")

    def test_sensor_loss_mid_drive_stops_with_fresh_lease_and_command(self):
        for lose, reason in ((lambda: setattr(self.nav, "frame_at", self.nav.frame_at - 2), "nav_depth_lost"),
                             (lambda: setattr(self.nav.mapping, "last_received", time.monotonic() - 1),
                              "nav_pose_lost")):
            with self.subTest(reason=reason):
                feed(self.nav)
                self.send(deadman=False)
                self.drive_goto()
                lose()
                self.bridge(type="cmd_vel", linear=0.2, angular=0.0, goal_id=self.nav.goal_id)
                self.assertEqual(self.state.output()[:2], (0, 0))
                status = self.state.status()
                self.assertEqual((status["reason"], status["requires_release"]), (reason, True))
                self.assertFalse(self.send(mode="goto"))  # needs an explicit release first
                feed(self.nav)
                self.send(deadman=False)
                self.assertTrue(self.send(mode="goto"))  # recovered sensors + a new press resume

    def test_sensor_session_change_drops_goal_command_and_depth(self):
        # Review 008 (2): MapState.start_session for a new phone session or map epoch.
        self.drive_goto()
        generation = self.nav.generation
        self.nav.mapping.start_session(("second", 2), "iPhone")
        self.bridge(type="cmd_vel", linear=0.2, angular=0.0, goal_id=self.nav.goal_id)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertEqual(self.nav.generation, generation + 1)
        self.assertIsNone(self.nav.goal)
        self.assertIsNone(self.nav.frame)
        self.assertIsNone(self.nav.cmd)
        self.assertEqual(self.nav.intent()["generation"], generation + 1)
        # A late frame of the old session never becomes depth authority.
        feed(self.nav, identity=("second", 2), frame=False)
        self.nav.offer_frame(type("Frame", (), dict(header=dict(session_id=PHONE[0], map_epoch=PHONE[1],
                                                                 tracking="normal")))())
        self.assertIsNone(self.nav.frame)

    def test_limited_tracking_is_never_navigation_authority(self):
        # Review 008 (3): a fresh pose with tracking "limited" or "not_available".
        self.drive_goto()
        for tracking in ("limited", "not_available"):
            with self.subTest(tracking=tracking):
                feed(self.nav, tracking=tracking)
                transform, _, source, _ = self.nav.pose_snapshot()
                self.assertIsNone(transform)
                self.assertEqual(source, "limited")
                self.assertIsNone(self.nav.frame)  # the limited frame also withdrew depth
                self.assertEqual(self.state.output()[:2], (0, 0))
                self.assertEqual(self.state.status()["reason"], "nav_tracking_limited")
                feed(self.nav)
                self.send(deadman=False)
                self.drive_goto()

    def begin_seek(self, target_id=4):
        packet = detected(self.nav.frame, boxes=[dict(id=target_id, cls="person", x=.45, y=.2, w=.1, h=.6)])
        self.state.detections = lambda: packet
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send(mode="seek", target_id=target_id))
        self.state.output()
        return self.nav.intent()["goal_id"]

    def test_seek_uses_exact_retained_capture_when_inference_finishes_after_newer_depth(self):
        original = self.nav.frame
        packet = detected(original)
        newest = feed(self.nav)
        newest.depth[:] = 6
        self.state.detections = lambda: packet
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send(mode="seek", target_id=4))
        self.state.output()
        expected = self.nav.locate(packet["boxes"][0], original)
        np.testing.assert_allclose(self.nav.seek["person"], expected)
        self.assertNotEqual(self.nav.seek["person"], self.nav.locate(packet["boxes"][0], newest))

    def test_invalid_seek_pair_stops_immediately_without_waiting_for_projection_throttle(self):
        goal = self.begin_seek()
        self.bridge(type="cmd_vel", linear=.2, angular=0, goal_id=goal)
        self.assertGreater(self.state.output()[0], 0)
        packet = detected(self.nav.frame)
        packet["sensor_key"][3] += 100
        self.state.detections = lambda: packet
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertIsNone(self.nav.cmd)
        self.assertIsNone(self.nav.intent()["goal"])

    def test_seek_rejects_missing_malformed_wrong_source_and_non_boolean_pair_metadata(self):
        for change in (dict(sensor_key=None), dict(sensor_key="unknown"),
                       dict(sensor_key=[*PHONE, True, 1, 1.]), dict(mirrored=None),
                       dict(mirrored=1), dict(source="PHONE RGB"), dict(fresh=1),
                       dict(frame=None), dict(frame=999), dict(frame=True)):
            with self.subTest(change=change):
                self.setUp()
                goal = self.begin_seek()
                self.bridge(type="cmd_vel", linear=.2, angular=0, goal_id=goal)
                self.assertGreater(self.state.output()[0], 0)
                packet = detected(self.nav.frame, **change)
                self.state.detections = lambda: packet
                self.assertEqual(self.state.output()[:2], (0, 0))
                self.assertIsNone(self.nav.cmd)
                self.assertIsNone(self.nav.intent()["goal"])
                self.nav.seek_checked = 0

    def test_seek_rejects_out_of_view_boxes_and_non_integer_person_ids_immediately(self):
        for change in (dict(x=-.1), dict(y=-.1), dict(x=1.1), dict(y=1.1),
                       dict(w=1.1), dict(h=1.1), dict(x=.9, w=.2), dict(y=.9, h=.2),
                       dict(id=4.0), dict(id=True)):
            with self.subTest(change=change):
                self.setUp()
                target_id = 1 if change.get("id") is True else 4
                goal = self.begin_seek(target_id)
                self.bridge(type="cmd_vel", linear=.2, angular=0, goal_id=goal)
                self.assertGreater(self.state.output()[0], 0)
                box = dict(id=target_id, cls="person", x=.45, y=.2, w=.1, h=.6)
                box.update(change)
                packet = detected(self.nav.frame, boxes=[box])
                self.state.detections = lambda: packet
                self.assertEqual(self.state.output()[:2], (0, 0))
                self.assertIsNone(self.nav.cmd)
                self.assertIsNone(self.nav.intent()["goal"])

    def test_same_id_and_capture_on_reconnected_socket_cannot_pair_old_inference(self):
        old = self.nav.frame
        packet = detected(old)
        self.nav.mapping.start_session(PHONE, "same phone")
        newest = feed(self.nav)
        newest.header.update(frame_id=old.header["frame_id"], t_capture=old.header["t_capture"])
        newest.header["_mapping_sensor_key"] = (*PHONE, self.nav.mapping.socket_generation,
                                                  old.header["frame_id"], old.header["t_capture"])
        self.nav.offer_frame(newest)
        self.state.detections = lambda: packet
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send(mode="seek", target_id=4))
        self.state.output()
        self.assertIsNone(self.nav.intent()["goal"])
        self.assertIsNone(self.nav.cmd)

    def test_seek_does_not_use_an_expired_older_capture_when_newest_depth_is_fresh(self):
        old = self.nav.frame
        old.header["_received_monotonic"] = time.monotonic() - 2
        self.nav.offer_frame(old)
        packet = detected(old)
        feed(self.nav)
        self.assertIsNone(self.nav.sensor_fault())
        self.state.detections = lambda: packet
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send(mode="seek", target_id=4))
        self.state.output()
        self.assertIsNone(self.nav.intent()["goal"])

    def test_seek_depth_cache_is_bounded_and_evicted_capture_cannot_drive(self):
        old = self.nav.frame
        packet = detected(old)
        for _ in range(13):
            feed(self.nav)
        self.state.detections = lambda: packet
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send(mode="seek", target_id=4))
        self.state.output()
        self.assertIsNone(self.nav.intent()["goal"])
        self.assertLessEqual(len(self.nav.seek_frames), 12)

    def test_seek_rotates_matched_sensor_depth_to_the_detector_input_view(self):
        from jetson.mapping.tests.test_orientation import phone_upside_down
        upright = simulated_frame(1.0, 4, identity=PHONE)
        sensor = phone_upside_down(upright)
        sensor.header.update(_received_monotonic=time.monotonic(),
                             _mapping_socket_generation=self.nav.mapping.socket_generation,
                             _mapping_sensor_key=(*PHONE, self.nav.mapping.socket_generation, 4,
                                                  sensor.header["t_capture"]))
        with self.nav.mapping.lock:
            self.nav.mapping.pose = sensor.transform.copy()
            self.nav.mapping.last_received = time.monotonic()
        self.nav.offer_frame(sensor)
        packet = detected(sensor, mirrored=True)
        packet["boxes"] = [dict(x=.31, y=.22, w=.2, h=.4, cls="person", id=4)]
        self.state.detections = lambda: packet
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send(mode="seek", target_id=4))
        self.state.output()
        expected = self.nav.locate(packet["boxes"][0], upright)
        np.testing.assert_allclose(self.nav.seek["person"], expected, atol=2e-3)

    def test_seek_projection_that_expires_cannot_rearm_a_command(self):
        self.begin_seek()
        locate = self.nav.locate
        clock = SimpleNamespace(now=time.monotonic())
        def slow_projection(box, frame):
            result = locate(box, frame)
            clock.now += 2
            self.nav.mapping.last_received = clock.now
            return result
        self.nav.locate = slow_projection
        self.nav.seek_checked = 0
        with patch('jetson.nav.time', SimpleNamespace(monotonic=lambda: clock.now)):
            self.nav.update_seek(detected(self.nav.frame))
        self.assertIsNone(self.nav.intent()["goal"])
        self.assertIsNone(self.nav.cmd)

    def test_seek_replacement_stops_until_a_command_for_the_new_goal_arrives(self):
        first = self.begin_seek()
        self.bridge(type="cmd_vel", linear=.2, angular=0, goal_id=first)
        self.assertGreater(self.state.output()[0], 0)
        self.nav.frame.depth[:] = 4
        self.nav.seek_checked = 0
        self.assertEqual(self.state.output()[:2], (0, 0))
        second = self.nav.intent()["goal_id"]
        self.assertNotEqual(second, first)
        self.bridge(type="cmd_vel", linear=-.2, angular=0, goal_id=first)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.bridge(type="cmd_vel", linear=.2, angular=0, goal_id=second)
        self.assertGreater(self.state.output()[0], 0)

    def test_sensor_reset_discards_seek_goal(self):
        self.begin_seek()
        self.nav.mapping.start_session(("second", 2), "iPhone")
        self.nav.sync_generation()
        self.assertIsNone(self.nav.intent()["goal"])
        self.assertIsNone(self.nav.seek)

    def test_same_identity_reconnect_discards_seek_and_its_command(self):
        goal_id = self.begin_seek()
        self.bridge(type="cmd_vel", linear=.2, angular=0, goal_id=goal_id)
        self.assertGreater(self.state.output()[0], 0)
        generation = self.nav.generation
        self.nav.mapping.start_session(PHONE, "same iPhone")
        feed(self.nav)
        self.assertEqual(self.nav.generation, generation + 1)
        self.assertIsNone(self.nav.seek)
        self.assertIsNone(self.nav.cmd)
        self.bridge(type="cmd_vel", linear=.2, angular=0, goal_id=goal_id, generation=generation)
        self.assertEqual(self.state.output()[:2], (0, 0))

    def test_same_identity_reconnect_disarms_goto(self):
        self.drive_goto()
        self.nav.mapping.start_session(PHONE, "same iPhone")
        feed(self.nav)
        self.assertIsNone(self.nav.goal)
        self.assertIsNone(self.nav.cmd)
        self.assertEqual(self.state.output()[:2], (0, 0))

    def test_seek_computed_before_sensor_reset_cannot_rearm_the_old_goal(self):
        self.begin_seek()
        locate = self.nav.locate

        def reset_during_projection(box, frame):
            person = locate(box, frame)
            self.nav.mapping.start_session(("second", 2), "iPhone")
            feed(self.nav, identity=("second", 2))
            return person

        self.nav.locate = reset_during_projection
        self.nav.seek_checked = 0
        self.state.output()
        self.assertIsNone(self.nav.seek)
        self.assertIsNone(self.nav.intent()["goal"])

    def test_explore_rejects_old_or_missing_command_generation_after_reset(self):
        self.bridge(type="status", state="driving", goal_id="explore-1")
        self.assertTrue(self.send(mode="explore"))
        self.state.output()
        old_generation = self.nav.generation
        self.nav.mapping.start_session(("second", 2), "iPhone")
        feed(self.nav, identity=("second", 2))
        self.bridge(type="cmd_vel", linear=.2, angular=0, goal_id="explore-1", generation=old_generation)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.nav.receive(dict(type="cmd_vel", linear=.2, angular=0, goal_id="explore-1"))
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.bridge(type="cmd_vel", linear=.2, angular=0, goal_id="explore-2")
        self.assertGreater(self.state.output()[0], 0)

    def test_old_generation_status_cannot_replace_the_current_path(self):
        self.drive_goto()
        self.bridge(type="status", state="driving", goal_id=self.nav.goal_id, path=[[0, 0], [1, 0]])
        current = self.nav.remote.copy()
        self.bridge(type="status", generation=self.nav.generation - 1,
                    state="arrived", goal_id=self.nav.goal_id, path=[[9, 9]])
        self.assertEqual(self.nav.remote, current)

    def test_depth_producer_delay_is_part_of_navigation_freshness(self):
        self.nav.frame.header["producer_delay_ms"] = 2000
        self.nav.offer_frame(self.nav.frame)
        self.assertEqual(self.nav.sensor_fault(), "nav_depth_lost")

    def test_late_old_session_frame_cannot_withdraw_current_depth(self):
        feed(self.nav, identity=("second", 2))
        current = self.nav.frame
        self.nav.offer_frame(type("Frame", (), dict(header=dict(session_id=PHONE[0], map_epoch=PHONE[1],
                                                                 tracking="normal")))())
        self.assertIs(self.nav.frame, current)

    def test_seek_projects_the_person_through_depth_and_follows_with_standoff(self):
        frame = feed(self.nav)
        depth = frame.depth
        boxes = [dict(id=4, cls="person", x=0.45, y=0.2, w=0.1, h=0.6)]
        packet = detected(frame, boxes=boxes, age_ms=10)
        self.state.detections = lambda: packet
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send(mode="seek", target_id=4))
        self.state.output()
        seek = self.nav.seek
        self.assertAlmostEqual(seek["person"][0], 0.0, places=1)
        self.assertAlmostEqual(seek["person"][1], -3.0, places=1)
        self.assertAlmostEqual(self.nav.intent()["goal"]["x"], 3.0 - self.nav.standoff, places=3)  # odom X = -z
        first = self.nav.intent()["goal_id"]
        self.assertTrue(first.startswith("seek-"))
        self.assertEqual(self.state.status()["nav"]["mode"], "seek")
        depth[:] = 4.0  # the person walked 1 m further away
        self.nav.seek_checked = 0
        self.state.output()
        self.assertNotEqual(self.nav.intent()["goal_id"], first)
        self.bridge(type="cmd_vel", linear=0.2, angular=0.0, goal_id=self.nav.intent()["goal_id"])
        self.assertGreater(self.state.output()[0], 0)
        depth[:] = 0.9  # close enough: arrived, stopped
        self.nav.seek_checked = 0
        self.state.output()
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertEqual(self.state.status()["nav"]["state"], "arrived")
        boxes.clear()  # out of view long enough: target lost
        self.nav.SEEK_LOST_S, self.nav.seek_checked = 0.0, 0
        self.state.output()
        self.assertEqual(self.state.status()["nav"]["state"], "failed")

    def test_nav_explore_keeps_exploring_when_a_person_is_seen(self):
        # Indoors a person is a moving obstacle for the costmaps, never a reason to switch modes.
        boxes = []
        self.state.detections = lambda: dict(boxes=boxes, fresh=True, age_ms=10, error=None)
        self.bridge(type="status", state="driving", goal_id="explore-1")
        self.assertTrue(self.send(mode="explore"))
        self.state.output()
        self.assertEqual(self.nav.intent()["mode"], "explore")
        boxes.append(dict(id=7, cls="person", x=0.4, y=0.2, w=0.1, h=0.5))
        for _ in range(3):
            self.state.output()
        self.assertEqual((self.state.status()["mode"], self.nav.intent()["mode"]), ("explore", "explore"))
        self.assertIsNone(self.nav.intent().get("target_id"))
        self.assertEqual(self.state.status()["nav"]["backend"], "nav2")

    def test_controller_change_or_serial_fault_disarms_the_goal(self):
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send())
        self.nav.set_goal(0.5, -1.0)
        self.state.end(self.session)
        self.assertIsNone(self.nav.goal)
        self.session = self.state.begin()
        self.send(deadman=False)
        self.assertTrue(self.send())
        self.nav.set_goal(0.5, -1.0)
        self.state.set_connected(False)
        self.assertIsNone(self.nav.goal)

    def test_goal_post_only_arms_and_never_moves_a_teleop_operator(self):
        self.bridge(type="status", state="idle", goal_id=None)
        self.assertTrue(self.send())
        old = self.state.status(issue_permit=True)["permit"]
        self.nav.set_goal(0.5, -1.0)  # e.g. another client on the network
        self.bridge(type="cmd_vel", linear=0.3, angular=0)
        self.seq += 1  # the operator's in-flight centred-stick teleop heartbeat, permit from before the POST
        self.assertTrue(self.state.accept(dict(session_id=self.session, permit=old, seq=self.seq, deadman=True, v=0,
                                               w=0, pan=0, mode="teleop", video_fresh=True, video_age_ms=0),
                                          self.session))
        self.assertEqual(self.state.status()["mode"], "teleop")
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertTrue(self.send(mode="goto"))
        self.assertEqual(self.state.output()[:2], (0, 0))  # the pre-press command is discarded
        self.bridge(type="cmd_vel", linear=0.3, angular=0, goal_id=self.nav.goal_id - 1)
        self.assertEqual(self.state.output()[:2], (0, 0))  # computed for an earlier goal
        self.bridge(type="cmd_vel", linear=0.3, angular=0, goal_id=self.nav.goal_id)
        self.assertGreater(self.state.output()[0], 0)
        self.assertTrue(self.send(mode="teleop"))
        self.assertEqual(self.state.status()["mode"], "teleop")
        self.assertIsNone(self.state.status()["nav"]["mode"])
        self.assertTrue(self.send(mode="explore"))  # another autonomy mode disarms the goal
        self.assertIsNone(self.state.status()["nav"]["goal"])

    def test_stick_override_and_arrival_stop(self):
        self.bridge(type="status", state="idle", goal_id=None)
        self.send()
        self.nav.set_goal(0.5, -1.0)
        self.send(mode="goto")
        goal_id = self.nav.intent()["goal_id"]
        self.bridge(type="cmd_vel", linear=0.2, angular=0)
        self.bridge(type="status", state="arrived", goal_id=goal_id)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertEqual(self.state.status()["nav"]["state"], "arrived")
        self.send(mode="goto", v=0.4)
        self.assertEqual(self.state.status()["autonomy_state"], "stick_override")
        self.assertEqual(self.state.output()[:2], (40, 40))

    def test_bridge_silence_holds_every_mode_without_reactive_fallback(self):
        self.bridge(type="status", state="idle", goal_id=None)
        self.send()
        self.nav.set_goal(0.5, -1.0)
        self.send(mode="goto")
        self.clock.now += 1.6
        self.send(deadman=False)
        self.send(mode="goto")
        self.bridge(type="cmd_vel", linear=0.2, angular=0)
        self.clock.now += 1.6  # bridge silent: commands are no longer trusted
        self.send(mode="goto")
        nav = self.state.status()["nav"]
        self.assertEqual((nav["state"], nav["backend"]), ("unavailable", None))
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.send(deadman=False)
        self.state.update_range(150)  # clear range would let the reactive policy drive
        self.send(mode="explore")
        nav = self.state.status()["nav"]
        self.assertEqual((nav["mode"], nav["backend"], nav["state"]), ("explore", None, "unavailable"))
        self.assertEqual(nav["message"], "Nav2 bridge silent; holding still")
        self.assertEqual(self.state.output()[:2], (0, 0))  # no blind timed turns
        self.bridge(type="status", state="idle", goal_id=None)  # bridge back: Nav2 drives again
        self.send(mode="explore")
        self.bridge(type="cmd_vel", linear=0.2, angular=0)
        self.assertEqual(self.state.status()["nav"]["backend"], "nav2")
        self.assertNotEqual(self.state.output()[:2], (0, 0))

    def test_command_caps_and_conversion(self):
        self.nav.receive(dict(type="cmd_vel", linear=10.0, angular=-50.0, generation=self.nav.generation))
        v, w = self.nav.cmd[:2]
        self.assertEqual((v, w), (self.nav.max_v, self.nav.max_w))
        with self.assertRaises(ValueError):
            self.nav.receive(dict(type="cmd_vel", linear=math.nan, angular=0))
        with self.assertRaises(ValueError):
            self.nav.receive(dict(type="teleport"))


class DemoSensorPauseTests(unittest.TestCase):
    """Demo AUTO: a short pose/depth/camera stall holds the motors and resumes the same Explore."""

    def setUp(self):
        self.clock = Clock()
        self.camera = [True]
        self.nav = NavService(MapState(), clock=self.clock)
        self.nav.bridge = object()
        feed(self.nav)
        self.state = ControlState(clock=self.clock, navigator=self.nav, proximity_stop=False, demo_mode=True,
                                  camera_ready=lambda: self.camera[0])
        self.nav.attach(self.state)
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.seq = 0
        self.send(deadman=False)
        self.nav.receive(dict(type="status", state="idle", goal_id=None, generation=self.nav.generation))

    def send(self, **values):
        self.seq += 1
        status = self.state.status(issue_permit=True)
        message = dict(session_id=self.session, permit=status["permit"], seq=self.seq, deadman=True,
                       v=0, w=0, pan=0, mode="explore", video_fresh=True, video_age_ms=0)
        message.update(values)
        return self.state.accept(message, self.session)

    def drive(self):
        self.assertTrue(self.send())
        self.state.output()
        self.nav.receive(dict(type="cmd_vel", linear=0.2, angular=0.0, generation=self.nav.generation,
                              goal_id=self.nav.intent()["goal_id"]))
        return self.state.output()[:2]

    def test_short_stalls_hold_still_and_resume_the_same_explore_without_a_press(self):
        for stall, reason in ((lambda: setattr(self.nav.mapping, "last_received", time.monotonic() - 1.5),
                               "nav_pose_lost"),
                              (lambda: setattr(self.nav, "frame_at", self.nav.frame_at - 2), "nav_depth_lost"),
                              (lambda: self.camera.__setitem__(0, False), "camera_stale")):
            with self.subTest(reason=reason):
                feed(self.nav)
                self.camera[0] = True
                self.assertGreater(self.drive()[0], 0)
                stall()
                self.assertEqual(self.state.output()[:2], (0, 0))
                status = self.state.status()
                self.assertEqual((status["mode"], status["reason"], status["requires_release"]),
                                 ("explore", reason, False))
                self.assertTrue(self.nav.intent()["active"])  # the bridge keeps its goal: no cancel, no rescan
                self.assertTrue(self.send(video_fresh=False, video_age_ms=5000))  # a held heartbeat still lands
                feed(self.nav)
                self.camera[0] = True
                self.assertGreater(self.drive()[0], 0)  # fresh sensors: same session, no release or re-press

    def test_a_long_stall_still_stops(self):
        self.assertGreater(self.drive()[0], 0)
        self.nav.mapping.last_received = time.monotonic() - 1.5
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.clock.now += self.state.SENSOR_PAUSE_S
        self.nav.receive(dict(type="status", state="driving", goal_id=None, generation=self.nav.generation))
        self.send()
        self.assertEqual(self.state.output()[:2], (0, 0))
        status = self.state.status()
        self.assertEqual((status["mode"], status["reason"]), ("teleop", "nav_pose_lost"))
        self.assertFalse(self.nav.intent()["active"])

    def test_strict_mode_still_stops_at_once(self):
        self.state.demo_mode = False
        self.assertGreater(self.drive()[0], 0)
        self.nav.mapping.last_received = time.monotonic() - 1.5
        self.assertEqual(self.state.output()[:2], (0, 0))
        status = self.state.status()
        self.assertEqual((status["reason"], status["requires_release"]), ("nav_pose_lost", True))


class TranslationFloorTests(unittest.TestCase):
    """A moving forward Nav2 translation is never slower than MIN_LINEAR_MPS; zero and reverse stay as sent."""

    def setUp(self):
        self.nav = NavService(MapState(), clock=Clock())
        self.nav.mode, self.nav.active, self.nav.bridge = "explore", True, object()
        feed(self.nav)

    def command(self, linear, angular):
        self.nav.receive(dict(type="cmd_vel", linear=linear, angular=angular, generation=self.nav.generation))
        return self.nav.command(si=True), self.nav.command()

    def test_slow_translations_rise_to_the_floor_on_the_same_arc(self):
        for (linear, angular), (v, w) in (((0.05, 0.1), (0.15, 0.3)), ((-0.03, 0.0), (-0.03, 0.0)),
                                          ((0.0, 1.2), (0.0, 1.2)), ((0.0, 0.0), (0.0, 0.0)),
                                          ((0.15, -0.4), (0.15, -0.4)), ((0.3, 0.5), (0.3, 0.5))):
            with self.subTest(linear=linear, angular=angular):
                si, normalized = self.command(linear, angular)
                self.assertAlmostEqual(si[0], v)
                self.assertAlmostEqual(si[1], w)
                self.assertAlmostEqual(normalized[0], v / self.nav.speed_mps)

    def test_the_floor_reaches_the_wheels_through_the_server(self):
        clock = Clock()
        nav = NavService(MapState(), clock=clock)
        nav.bridge = object()
        feed(nav)
        state = ControlState(clock=clock, navigator=nav, proximity_stop=False)
        nav.attach(state)
        state.set_connected(True)
        session = state.begin()
        for seq, deadman in ((1, False), (2, True)):
            permit = state.status(issue_permit=True)["permit"]
            state.accept(dict(session_id=session, permit=permit, seq=seq, deadman=deadman, v=0, w=0, pan=0,
                              mode="explore", video_fresh=True, video_age_ms=0), session)
        state.output()
        nav.receive(dict(type="status", state="driving", goal_id=None, generation=nav.generation))
        nav.receive(dict(type="cmd_vel", linear=0.02, angular=0.0, generation=nav.generation))
        left, right, _ = state.output()
        self.assertEqual((left, right), (round(0.15 / nav.speed_mps * state.max_pwm),) * 2)


class NavLaneTests(unittest.TestCase):
    def test_arrays_only_on_change_at_most_twice_a_second_with_a_refresh(self):
        clock = Clock()
        lane = NavLane(clock=clock)

        def status(revision, path):
            return dict(type="control_status", permit="p", nav=dict(state="driving", goal=None, path=path,
                                                                    frontiers=[[1.0, 1.0]], revision=revision))
        self.assertEqual(lane.apply(status(1, [[0, 0]]))["nav"]["path"], [[0, 0]])  # the first status carries them
        clock.now += 0.025
        lean = lane.apply(status(1, [[0, 0]]))
        self.assertEqual(set(lean["nav"]), {"state", "goal", "revision"})
        self.assertEqual((lean["permit"], lean["nav"]["revision"]), ("p", 1))
        clock.now += 0.1
        self.assertNotIn("path", lane.apply(status(2, [[0, 1]]))["nav"])  # changed, but within 0.5 s
        clock.now += 0.4
        changed = lane.apply(status(3, [[0, 2]]))["nav"]  # coalesced: the newest arrays
        self.assertEqual((changed["path"], changed["frontiers"], changed["revision"]), ([[0, 2]], [[1.0, 1.0]], 3))
        clock.now += 1.9
        self.assertNotIn("path", lane.apply(status(3, [[0, 2]]))["nav"])
        clock.now += 0.2
        self.assertIn("path", lane.apply(status(3, [[0, 2]]))["nav"])  # 2 s refresh
        plain = dict(type="control_status")
        self.assertIs(lane.apply(plain), plain)


class ServerTests(unittest.TestCase):
    def test_control_socket_nav_lane_is_opt_in(self):
        app = create_app(demo=True, simulate_rover=True, detector="none", nav2=True, proximity_stop=False)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/control") as ws:
                status = ws.receive_json()
                self.assertTrue(all("path" in ws.receive_json()["nav"] for _ in range(4)))  # legacy: every status
                ws.send_json(dict(session_id=status["session_id"], seq=0, deadman=False, nav_lane="revision"))
                statuses = [ws.receive_json() for _ in range(16)]
                tail = statuses[-6:]
                self.assertTrue(all("path" not in s["nav"] and "frontiers" not in s["nav"] for s in tail))
                self.assertTrue(all(isinstance(s["nav"]["revision"], int) and s["permit"] for s in tail))
            self.assertIn("path", client.get("/nav/status").json())  # the HTTP snapshot stays complete
            self.assertIn("revision", client.get("/control/status").json()["nav"])

    def test_disabled_by_default_contract_answers_without_navigation(self):
        app = create_app(demo=True, simulate_rover=True, detector="none")
        with TestClient(app) as client:
            self.assertNotIn("nav", client.get("/control/status").json())
            refused = client.post("/nav/goal", json=dict(x=1, z=1))
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(set(refused.json()), {"nav", "message"})
            self.assertEqual((refused.json()["nav"]["state"], refused.json()["nav"]["backend"]), ("unavailable", None))
            cancelled = client.post("/nav/cancel")
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(set(cancelled.json()), NAV_KEYS)
            self.assertEqual((cancelled.json()["state"], cancelled.json()["backend"]), ("idle", None))
            self.assertNotIn("nav", client.get("/health").json())  # shape-identical to the Rust server

    def test_bridge_socket_streams_pose_depth_intent_and_drives_goto(self):
        app = create_app(demo=True, simulate_rover=True, detector="none", nav2=True, proximity_stop=False)
        with TestClient(app) as client:
            nav = client.get("/control/status").json()["nav"]
            self.assertEqual((nav["state"], nav["backend"]), ("unavailable", None))
            refused = client.post("/nav/goal", json=dict(x=1, z=1))
            self.assertEqual(refused.status_code, 409)
            self.assertEqual(refused.json()["nav"]["state"], "unavailable")
            with client.websocket_connect("/ws/nav") as bridge:
                bridge.send_json(dict(type="hello", version=1, node="test"))
                hello = bridge.receive_json()
                self.assertEqual((hello["type"], hello["speed_mps"]), ("hello", 0.45))
                seen = set()
                deadline = time.monotonic() + 5
                while seen != {"pose", "depth", "range", "intent"} and time.monotonic() < deadline:
                    message = bridge.receive()
                    if message.get("bytes"):
                        header, depth = decode_depth(message["bytes"])
                        self.assertEqual(header["source"], "simulated")
                        seen.add("depth")
                    elif message.get("text"):
                        import json
                        seen.add(json.loads(message["text"])["type"])
                self.assertEqual(seen, {"pose", "depth", "range", "intent"})
                bridge.send_json(dict(type="status", state="idle", goal_id=None, generation=hello["generation"]))
                time.sleep(0.05)
                self.assertTrue(client.get("/health").json()["nav"]["available"])
                with client.websocket_connect("/ws/control") as ws:
                    status = ws.receive_json()
                    ws.send_json(dict(session_id=status["session_id"], seq=0, deadman=False))
                    status = ws.receive_json()
                    staged = client.post("/nav/goal", json=dict(x=0.2, z=-1.0))  # stopped: staged, not refused
                    self.assertEqual(staged.status_code, 202, staged.text)
                    self.assertEqual((staged.json()["mode"], staged.json()["goal"]), (None, {"x": 0.2, "z": -1.0}))
                    self.assertEqual(client.get("/control/status").json()["left_pwm"], 0)
                    ws.send_json(dict(session_id=status["session_id"], permit=status["permit"], seq=1, deadman=True,
                                      v=0, w=0, pan=0, mode="teleop", video_fresh=True, video_age_ms=0))
                    time.sleep(0.03)
                    reply = client.post("/nav/goal", json=dict(x=0.4, z=-1.5))
                    self.assertEqual(reply.status_code, 202, reply.text)
                    self.assertEqual(reply.json()["mode"], "goto")
                    for seq in range(2, 40):
                        status = ws.receive_json()
                        ws.send_json(dict(session_id=status["session_id"], permit=status["permit"], seq=seq,
                                          deadman=True, v=0, w=0, pan=0, mode="goto", video_fresh=True, video_age_ms=0))
                        if status["mode"] == "goto":
                            bridge.send_json(dict(type="cmd_vel", linear=0.2, angular=0.0,
                                                  generation=app.state.nav.generation, goal_id=app.state.nav.goal_id))
                            if status["left_pwm"] > 0:
                                break
                    self.assertEqual(status["mode"], "goto")
                    self.assertGreater(status["left_pwm"], 0)
                    self.assertEqual(status["nav"]["goal"], {"x": 0.4, "z": -1.5})
            cancelled = client.post("/nav/cancel").json()
            self.assertIsNone(cancelled["mode"])

    def test_sensor_session_change_sends_bridge_reset_and_retags_samples(self):
        app = create_app(demo=True, simulate_rover=True, detector="none", nav2=True, proximity_stop=False)
        with TestClient(app) as client:
            with client.websocket_connect("/ws/nav") as bridge:
                bridge.send_json(dict(type="hello", version=1, node="test"))
                generation = bridge.receive_json()["generation"]
                app.state.nav.mapping.start_session(("second", 2), "iPhone")
                deadline, reset = time.monotonic() + 5, None
                import json
                while reset is None and time.monotonic() < deadline:
                    message = bridge.receive()
                    if message.get("text"):
                        data = json.loads(message["text"])
                        if data["type"] == "reset":
                            reset = data
                        elif data["type"] in ("pose", "intent"):
                            self.assertEqual(data["generation"], generation)
                self.assertEqual(reset["generation"], generation + 1)

    def test_bridge_must_be_loopback_and_single(self):
        app = create_app(demo=True, simulate_rover=True, detector="none", nav2=True)
        with TestClient(app, client=("10.0.0.7", 5000)) as client:
            with client.websocket_connect("/ws/nav") as ws:
                with self.assertRaises(Exception):
                    ws.receive_json()


if __name__ == "__main__":
    unittest.main()
