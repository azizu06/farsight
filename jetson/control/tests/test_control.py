import math
import time
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from jetson.control import ControlService
from jetson.control.state import ControlState


class Clock:
    def __init__(self):
        self.now = 10.0

    def __call__(self):
        return self.now


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.state = ControlState(clock=self.clock, proximity_stop=True)
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.state.update_range(100)
        self.seq = 0
        self.send(deadman=False)

    def send(self, **values):
        self.seq += 1
        status = self.state.status(issue_permit=True)
        message = {"session_id": self.session, "permit": status["permit"],
                   "seq": self.seq, "deadman": True, "v": 0.5, "w": 0,
                   "pan": 0, "mode": "teleop", "video_fresh": True, "video_age_ms": 0}
        message.update(values)
        return self.state.accept(message, self.session)

    def test_active_drive_then_deadman_stop(self):
        self.assertTrue(self.send())
        self.assertEqual(self.state.output()[:2], (50, 50))
        self.send(deadman=False)
        self.assertEqual(self.state.output()[:2], (0, 0))

    def test_applied_motion_hook_reports_driven_command_and_forward_blocks(self):
        applied = []
        self.state.on_output = lambda v, w, blocked: applied.append((v, w, blocked))
        self.assertTrue(self.send(v=0.5, w=0.25))
        self.state.output()
        self.assertEqual(applied[-1], (0.5, 0.25, False))
        self.state.update_range(10)
        self.assertTrue(self.send(v=0.5, w=0))
        self.state.output()
        self.assertEqual(applied[-1], (0, 0.0, True))
        self.assertTrue(self.send(v=-0.5, w=0))  # reversing away is not a block
        self.state.output()
        self.assertEqual(applied[-1], (-0.5, 0.0, False))
        self.assertTrue(self.send(v=1, w=1))  # saturated wheels: applied command is normalised
        self.state.update_range(100)
        self.state.output()
        self.assertEqual(applied[-1], (0.5, 0.5, False))

    def test_stale_command_does_not_gain_fresh_lease(self):
        token = self.state.status(issue_permit=True)["permit"]
        self.clock.now += 0.2
        self.assertTrue(self.send(permit=token))
        self.assertEqual(self.state.output()[:2], (50, 50))
        self.clock.now += 0.051
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertTrue(self.state.requires_release)

    def test_expired_permit_and_replay_stop(self):
        token = self.state.status(issue_permit=True)["permit"]
        self.send()
        self.clock.now += 0.251
        self.assertFalse(self.send(permit=token))
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertFalse(self.send())  # new permit cannot replace physical release
        self.send(deadman=False)
        self.assertTrue(self.send())
        self.assertFalse(self.send(seq=self.seq - 1))
        self.assertEqual(self.state.output()[:2], (0, 0))

    def test_obstacle_blocks_forward_but_allows_turn_and_reverse(self):
        self.state.update_range(15)
        self.send(v=0.7, w=0.3)
        self.assertEqual(self.state.output()[:2], (30, -30))
        self.send(v=-0.5, w=0)
        self.assertEqual(self.state.output()[:2], (-50, -50))

    def test_missing_stale_and_off_axis_range(self):
        self.state.update_range(-1)
        self.send()
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.state.update_range(100)
        self.send(pan=40)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.send(pan=0)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.state.update_range(100)
        self.assertEqual(self.state.output()[:2], (50, 50))
        self.clock.now += 0.351
        self.send()
        self.assertEqual(self.state.output()[:2], (0, 0))

    def test_reconnection_and_new_session_cannot_reuse_commands(self):
        token = self.state.status(issue_permit=True)["permit"]
        self.send()
        self.state.set_connected(False)
        self.state.set_connected(True)
        self.state.update_range(100)
        self.assertFalse(self.send(permit=token))
        self.assertFalse(self.send())
        self.send(deadman=False)
        self.assertTrue(self.send())
        self.state.end(self.session)
        self.assertEqual(self.state.output()[:2], (0, 0))
        old = self.session
        self.session = self.state.begin()
        self.assertNotEqual(old, self.session)
        self.assertFalse(self.send(session_id=old))

    def test_invalid_commands_video_and_seek_all_stop(self):
        for invalid in ({"v": math.nan}, {"v": 1.1}, {"w": "1"}, {"pan": 90},
                        {"mode": "seek"}, {"video_fresh": False}, {"video_age_ms": 501},
                        {"video_age_ms": math.inf}, {"permit": []}, {"seq": True}):
            with self.subTest(invalid=invalid):
                self.send(deadman=False)
                self.send()
                self.assertFalse(self.send(**invalid))
                self.assertEqual(self.state.output()[:2], (0, 0))

    def test_mixer_bounds_and_right_turn_convention(self):
        self.send(v=1, w=1)
        self.assertEqual(self.state.output()[:2], (100, 0))
        self.send(v=0, w=1)
        self.assertEqual(self.state.output()[:2], (100, -100))

    def test_optional_pwm_floor_preserves_zero_reverse_arcs_and_ceiling(self):
        self.state.min_pwm = 60
        self.send(v=0, w=0)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.send(v=-0.5, w=0)
        self.assertEqual(self.state.output()[:2], (-80, -80))
        self.send(v=0.5, w=0.25)
        self.assertEqual(self.state.output()[:2], (90, 70))
        self.send(v=1, w=1)
        self.assertEqual(self.state.output()[:2], (100, 0))
        self.send(v=0, w=-1)
        self.assertEqual(self.state.output()[:2], (-100, 100))
        self.send(v=0.01, w=0)
        self.assertEqual(self.state.output()[:2], (60, 60))
        self.send(deadman=False)
        self.assertEqual(self.state.output()[:2], (0, 0))

    def test_pwm_floor_remains_stopped_when_forward_blocked(self):
        self.state.min_pwm = 60
        self.state.update_range(10)
        self.send(v=0.5, w=0)
        self.assertEqual(self.state.output()[:2], (0, 0))

    def test_pwm_configuration_validation_and_service_forwarding(self):
        for floor, ceiling in ((-1, 100), (100, 100), (101, 100), (0, 256),
                               (0, 0), (False, 100), (1.5, 100), (0, 100.5)):
            with self.subTest(floor=floor, ceiling=ceiling), self.assertRaises(ValueError):
                ControlState(min_pwm=floor, max_pwm=ceiling)
        service = ControlService(min_pwm=70, max_pwm=110)
        self.assertEqual(service.status()["min_pwm"], 70)
        self.assertEqual(service.status()["max_pwm"], 110)
        self.assertEqual(service.state.output()[:2], (0, 0))

    def test_server_camera_guard_stops_motion_and_requires_release(self):
        ready = [True]
        self.state.camera_ready = lambda: ready[0]
        self.send(v=-0.5)
        self.assertEqual(self.state.output()[:2], (-50, -50))
        ready[0] = False
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertTrue(self.state.requires_release)
        self.assertEqual(self.state.reason, "camera_stale")
        ready[0] = True
        self.assertFalse(self.send())
        self.send(deadman=False)
        self.assertTrue(self.send())

    def test_camera_guard_exception_fails_stopped(self):
        def failed():
            raise RuntimeError("camera worker failed")
        self.state.camera_ready = failed
        self.assertFalse(self.send())
        self.assertEqual(self.state.output()[:2], (0, 0))

    def test_second_owner_cannot_stop_first(self):
        self.send()
        self.assertIsNone(self.state.begin())
        self.state.end("other")
        self.assertEqual(self.state.output()[:2], (50, 50))

    def test_stale_release_stops_but_does_not_unlock(self):
        self.send()
        self.send(deadman=False, seq=0)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertTrue(self.state.requires_release)
        self.assertFalse(self.send())


class ProximityStopOffTests(unittest.TestCase):
    """Explicit opt-out: ultrasonic range never blocks; every other stop remains."""

    def setUp(self):
        self.clock = Clock()
        self.state = ControlState(clock=self.clock, proximity_stop=False)
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.seq = 0
        self.send(deadman=False)

    def send(self, **values):
        self.seq += 1
        status = self.state.status(issue_permit=True)
        message = {"session_id": self.session, "permit": status["permit"], "seq": self.seq,
                   "deadman": True, "v": 0.5, "w": 0, "pan": 0, "mode": "teleop",
                   "video_fresh": True, "video_age_ms": 0}
        message.update(values)
        return self.state.accept(message, self.session)

    def test_close_missing_and_stale_range_do_not_block_forward(self):
        self.assertFalse(self.state.proximity_stop)
        self.assertTrue(ControlState().proximity_stop)  # on unless explicitly disabled
        for reading in (10, None, "stale"):
            with self.subTest(reading=reading):
                if reading == "stale":
                    self.state.update_range(100)
                    self.clock.now += 1  # range goes stale; the lapsed lease needs a new press
                    self.send(deadman=False)
                elif reading is None:
                    self.state.update_range(-1)
                else:
                    self.state.update_range(reading)
                self.assertTrue(self.send())
                self.assertEqual(self.state.output()[:2], (50, 50))
                self.assertFalse(self.state.status()["estop"])

    def test_pan_rule_deadman_lease_and_camera_stops_remain(self):
        self.assertTrue(self.send(pan=30))
        self.assertEqual(self.state.output()[:2], (0, 0))  # off-axis mount still blocks forward
        self.assertTrue(self.state.status()["estop"])
        self.assertTrue(self.send(pan=0))
        self.assertEqual(self.state.output()[:2], (50, 50))
        self.clock.now += .26
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertEqual(self.state.reason, "lease_expired")
        self.send(deadman=False)
        self.state.camera_ready = lambda: False
        self.assertFalse(self.send())
        self.assertEqual(self.state.output()[:2], (0, 0))


class NoPanMountTests(unittest.TestCase):
    """--pan-mount none: the headset pans digitally; nothing else about the gates changes."""

    def setUp(self):
        self.clock = Clock()
        self.state = ControlState(clock=self.clock, proximity_stop=False, pan_mount="none")
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.seq = 0
        self.send(deadman=False)

    send = PolicyTests.send

    def test_commanded_pan_never_blocks_forward_or_moves_a_servo(self):
        self.assertTrue(self.send(pan=40))
        self.assertEqual(self.state.output(), (50, 50, 90))
        self.assertTrue(self.send(pan=-80, v=0.5, w=0.25))
        left, right, pan = self.state.output()
        self.assertGreater(left, right)
        self.assertEqual(pan, 90)
        self.assertEqual(self.state.status()["pan_deg"], 90)
        self.assertFalse(self.state.status()["estop"])

    def test_pan_is_still_validated_and_every_stop_remains(self):
        self.assertFalse(self.send(pan=90))  # outside -80..80 is still an invalid command
        self.assertEqual(self.state.reason, "invalid_command")
        self.send(deadman=False)
        self.assertTrue(self.send(pan=40))
        self.clock.now += .26
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertEqual(self.state.reason, "lease_expired")
        self.send(deadman=False)
        self.assertTrue(self.send(pan=40))
        self.send(deadman=False)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.state.camera_ready = lambda: False
        self.assertFalse(self.send(pan=40))
        self.assertEqual(self.state.output()[:2], (0, 0))

    def test_unknown_mount_is_rejected(self):
        with self.assertRaises(ValueError):
            ControlState(pan_mount="gimbal")


class ScanStartRaceTests(unittest.TestCase):
    """The operator's last teleop heartbeat (WebSocket) and POST /scan (HTTP)
    carry the same permit but travel separately, so either may arrive first."""

    def setUp(self):
        self.clock = Clock()
        self.state = ControlState(clock=self.clock)
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.state.update_range(100)
        self.seq = 0
        self.send(deadman=False)
        self.send()
        self.permit = self.state.status(issue_permit=True)["permit"]  # held by the client for both paths

    def send(self, permit=None, **values):
        self.seq += 1
        if permit is None:
            permit = self.state.status(issue_permit=True)["permit"]
        message = {"session_id": self.session, "permit": permit, "seq": self.seq,
                   "deadman": True, "v": 0, "w": 0, "pan": 0, "mode": "teleop",
                   "video_fresh": True, "video_age_ms": 0}
        message.update(values)
        return self.state.accept(message, self.session)

    def assert_sweeping(self, token):
        self.state.scan_command(token, w=.3)
        self.assertGreater(self.state.output()[0], 0)
        self.assertEqual(self.state.status()["mode"], "scan")

    def assert_cancelled(self, token, reason=None, wheels=(0, 0)):
        """Stopped with `reason`, or (override) handed to the operator's teleop command."""
        self.assertEqual(self.state.output()[:2], wheels)
        if reason:
            self.assertEqual(self.state.reason, reason)
        with self.assertRaises(ValueError):
            self.state.scan_command(token, w=.3)
        self.assertNotEqual(self.state.status()["mode"], "scan")

    def test_heartbeat_then_begin_scan_keeps_scan(self):
        self.assertTrue(self.send(permit=self.permit))
        token = self.state.begin_scan(self.session, self.permit)
        self.assert_sweeping(token)
        self.clock.now += .05
        self.assertTrue(self.send(mode="scan"))
        self.assert_sweeping(token)

    def test_begin_scan_then_zero_teleop_heartbeat_keeps_scan(self):
        token = self.state.begin_scan(self.session, self.permit)
        self.clock.now += .02
        self.assertTrue(self.send(permit=self.permit))
        self.assert_sweeping(token)
        self.assertEqual(self.state.reason, "active")
        self.clock.now += .05
        self.assertTrue(self.send(mode="scan"))
        self.assert_sweeping(token)

    def test_racing_heartbeat_cannot_steer_pan_or_outlive_its_permit(self):
        token = self.state.begin_scan(self.session, self.permit)
        self.clock.now += .2
        self.assertTrue(self.send(permit=self.permit, pan=40))
        self.assertEqual(self.state.pan, 0)
        self.assert_sweeping(token)
        self.clock.now += .051  # past the permit, though under 250 ms since the heartbeat
        self.assert_cancelled(token, "lease_expired")
        self.assertTrue(self.state.requires_release)

    def test_racing_heartbeat_with_stick_input_still_cancels(self):
        for values, wheels in (({"v": .2}, (20, 20)), ({"w": -.1}, (-10, 10)), ({"mode": "seek"}, (0, 0))):
            with self.subTest(**values):
                self.setUp()
                token = self.state.begin_scan(self.session, self.permit)
                self.send(permit=self.permit, **values)
                self.assert_cancelled(token, wheels=wheels)

    def test_racing_release_or_stale_view_still_cancels(self):
        for values, reason in (({"deadman": False}, "deadman_released"),
                               ({"video_age_ms": 501}, "video_stale"),
                               ({"video_fresh": False}, "video_stale")):
            with self.subTest(reason=reason, **values):
                self.setUp()
                token = self.state.begin_scan(self.session, self.permit)
                self.send(permit=self.permit, **values)
                self.assert_cancelled(token, reason)
                self.assertFalse(self.send(mode="scan"))  # no scan to resume

    def test_teleop_heartbeat_with_a_post_scan_permit_still_cancels(self):
        token = self.state.begin_scan(self.session, self.permit)
        self.clock.now += .02
        self.send()  # the client never entered scan mode (e.g. lost POST reply)
        self.assert_cancelled(token)

    def test_teleop_heartbeat_after_scan_mode_still_cancels(self):
        token = self.state.begin_scan(self.session, self.permit)
        self.assertTrue(self.send(mode="scan"))
        self.send(permit=self.permit)
        self.assert_cancelled(token)


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.service = ControlService(simulate=True)
        self.service.state.set_connected(True)
        self.service.state.update_range(100)
        app = FastAPI()
        app.include_router(self.service.router)
        self.client = TestClient(app)

    def wait_status(self, ws, predicate, timeout=1):
        deadline = time.monotonic() + timeout
        latest = None
        while time.monotonic() < deadline:
            latest = ws.receive_json()
            if time.monotonic() >= deadline:
                break
            if predicate(latest):
                return latest
        self.fail(f"Authoritative control status timed out: {latest}")

    def test_socket_drive_release_disconnect_and_single_owner(self):
        with self.client.websocket_connect("/ws/control") as ws:
            status = ws.receive_json()
            self.assertTrue(status["simulated"])
            self.assertTrue(status["requires_release"])
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect("/ws/control") as other:
                    other.receive_json()
            ws.send_json({"session_id": status["session_id"], "seq": 0, "deadman": False})
            status = self.wait_status(ws, lambda packet: packet["seq"] == 0)
            self.assertEqual(status["reason"], "deadman_released")
            self.assertFalse(status["requires_release"])
            ws.send_json({"session_id": status["session_id"], "permit": status["permit"],
                          "seq": 1, "deadman": True, "v": 0.5, "w": 0,
                          "video_fresh": True, "video_age_ms": 50})
            active = self.wait_status(ws, lambda packet: packet["seq"] == 1)
            self.assertEqual(active["left_pwm"], 50)
            self.assertEqual(active["right_pwm"], 50)
            self.assertEqual(self.service.state.output()[:2], (50, 50))
        self.assertEqual(self.service.state.output()[:2], (0, 0))
        self.assertIsNone(self.service.state.session_id)

    def test_malformed_json_stops_and_public_status_cannot_issue_permit(self):
        with self.client.websocket_connect("/ws/control") as ws:
            ws.receive_json()
            ws.send_text("not json")
            status = self.wait_status(ws, lambda packet: packet["reason"] == "invalid_json")
            self.assertEqual(status["reason"], "invalid_json")
            self.assertEqual((status["left_pwm"], status["right_pwm"]), (0, 0))
            self.assertTrue(status["requires_release"])
            public = self.client.get("/control/status").json()
            self.assertNotIn("session_id", public)
            self.assertIsNone(public["permit"])


if __name__ == "__main__":
    unittest.main()


class DemoModeTests(unittest.TestCase):
    """--demo-mode: transient stops still stop, but the next fresh held heartbeat resumes."""

    def setUp(self):
        self.clock = Clock()
        self.camera = [True]
        self.state = ControlState(clock=self.clock, proximity_stop=False, demo_mode=True,
                                  camera_ready=lambda: self.camera[0])
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.seq = 0
        self.send(deadman=False)  # the first press after start still needs one release

    def send(self, session=None, **values):
        self.seq += 1
        status = self.state.status(issue_permit=True)
        message = {"session_id": session or self.session, "permit": status["permit"],
                   "seq": self.seq, "deadman": True, "v": 0.5, "w": 0,
                   "pan": 0, "mode": "teleop", "video_fresh": True, "video_age_ms": 0}
        message.update(values)
        return self.state.accept(message, session or self.session)

    def assert_resumes(self):
        self.assertFalse(self.state.status()["requires_release"])
        self.assertTrue(self.send())
        self.assertEqual(self.state.output()[:2], (50, 50))

    def test_serial_blip_stops_then_resumes_without_a_new_press(self):
        self.assertTrue(self.send())
        self.state.set_connected(False, "serial_disconnected")
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.state.set_connected(True)
        self.assert_resumes()

    def test_controller_reconnect_resumes_without_a_release(self):
        self.assertTrue(self.send())
        self.state.end(self.session)
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.session = self.state.begin()
        self.seq = 0
        self.assert_resumes()

    def test_lease_expiry_still_stops_then_a_fresh_heartbeat_resumes(self):
        self.assertTrue(self.send())
        self.clock.now += self.state.LEASE + 0.01
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertEqual(self.state.status()["reason"], "lease_expired")
        self.assert_resumes()

    def test_teleop_ignores_camera_freshness_but_autonomy_does_not(self):
        self.camera[0] = False
        self.assertTrue(self.send(video_fresh=False, video_age_ms=2000))
        self.assertEqual(self.state.output()[:2], (50, 50))
        self.assertFalse(self.send(mode="explore", v=0))
        self.assertEqual(self.state.status()["reason"], "camera_stale")
        self.assertFalse(self.state.status()["requires_release"])

    def test_protocol_faults_and_releases_still_latch(self):
        self.assertTrue(self.send())
        self.assertFalse(self.send(v=5))
        self.assertEqual(self.state.status()["reason"], "invalid_command")
        self.assertTrue(self.state.status()["requires_release"])
        self.assertFalse(self.send())
        self.send(deadman=False)
        self.assertTrue(self.send())

    def test_off_by_default_keeps_latching(self):
        state = ControlState(clock=self.clock, proximity_stop=False)
        state.set_connected(True)
        state.begin()
        state.set_connected(False, "serial_disconnected")
        state.set_connected(True)
        self.assertTrue(state.status()["requires_release"])


class StaleSessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.state = ControlState(clock=self.clock, proximity_stop=False)
        self.state.set_connected(True)
        self.old = self.state.begin()

    def test_a_live_controller_keeps_the_rover(self):
        self.clock.now += 0.5
        self.state.accept({"session_id": self.old, "seq": 1, "deadman": False}, self.old)
        self.clock.now += 0.9
        self.assertIsNone(self.state.begin())  # refused: the owner spoke 0.9 s ago

    def test_a_silent_session_is_replaced_and_its_commands_are_refused(self):
        self.clock.now += self.state.STALE_SESSION + 0.01
        new = self.state.begin()
        self.assertIsNotNone(new)
        self.assertNotEqual(new, self.old)
        self.assertEqual(self.state.session_id, new)
        self.assertTrue(self.state.status()["requires_release"])
        self.assertFalse(self.state.accept({"session_id": self.old, "seq": 2, "deadman": True}, self.old))
        self.state.end(self.old)  # the old handler's cleanup must not end the new session
        self.assertEqual(self.state.session_id, new)
