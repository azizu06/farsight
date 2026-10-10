"""Server side of the Nav2 seam (jetson/nav.py): contract, lease gating, bridge protocol."""

import math
import time
import unittest

import numpy as np
from fastapi.testclient import TestClient

from jetson.control.state import ControlState
from jetson.mapping.demo import simulated_frame
from jetson.mapping.server import create_app
from jetson.mapping.state import MapState
from jetson.nav import NavService, Refused, decode_depth, encode_depth, odom_to_world, simplify, world_to_odom

NAV_KEYS = {"state", "mode", "goal", "path", "frontiers", "backend", "message"}
PHONE = ("phone-session", 1)


def feed(nav, tracking="normal", identity=PHONE, frame=True):
    """A fresh ARKit pose (and, with ``frame``, a LiDAR depth frame) of one sensor session."""
    mapping = nav.mapping
    with mapping.lock:
        mapping.identity, mapping.connected, mapping.tracking = identity, True, tracking
        mapping.last_received, mapping.pose = time.monotonic(), np.eye(4)
    if frame:
        nav.offer_frame(type("Frame", (), dict(header=dict(
            session_id=identity[0], map_epoch=identity[1], tracking=tracking,
            _received_monotonic=time.monotonic())))())


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


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
        self.nav.receive(message)

    def test_goal_needs_backend_and_live_permit(self):
        self.nav.bridge = None
        with self.assertRaises(Refused) as missing:
            self.nav.set_goal(1, -2)
        self.assertEqual((missing.exception.nav["state"], missing.exception.nav["backend"]), ("unavailable", None))
        self.nav.bridge = object()
        self.bridge(type="status", state="idle", goal_id=None)
        self.clock.now += 0.3  # no held lease
        with self.assertRaises(Refused) as unheld:
            self.nav.set_goal(1, -2)
        self.assertIn("permit", unheld.exception.message)
        self.assertEqual(set(unheld.exception.nav), NAV_KEYS)

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

    def test_bridge_silence_falls_back_and_explore_uses_reactive_policy(self):
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
        self.state.update_range(150)
        self.send(mode="explore")
        nav = self.state.status()["nav"]
        self.assertEqual((nav["mode"], nav["backend"], nav["state"]), ("explore", "fallback", "driving"))
        self.assertEqual(self.state.output()[:2], (25, 25))  # reactive Explore, unchanged

    def test_command_caps_and_conversion(self):
        self.nav.receive(dict(type="cmd_vel", linear=10.0, angular=-50.0))
        v, w = self.nav.cmd[:2]
        self.assertEqual((v, w), (self.nav.max_v, self.nav.max_w))
        with self.assertRaises(ValueError):
            self.nav.receive(dict(type="cmd_vel", linear=math.nan, angular=0))
        with self.assertRaises(ValueError):
            self.nav.receive(dict(type="teleport"))


class ServerTests(unittest.TestCase):
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
                bridge.send_json(dict(type="status", state="idle", goal_id=None))
                time.sleep(0.05)
                self.assertTrue(client.get("/health").json()["nav"]["available"])
                with client.websocket_connect("/ws/control") as ws:
                    status = ws.receive_json()
                    ws.send_json(dict(session_id=status["session_id"], seq=0, deadman=False))
                    status = ws.receive_json()
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
                            bridge.send_json(dict(type="cmd_vel", linear=0.2, angular=0.0))
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
