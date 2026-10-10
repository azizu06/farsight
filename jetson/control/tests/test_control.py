import math
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
        self.state = ControlState(clock=self.clock)
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


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.service = ControlService(simulate=True)
        self.service.state.set_connected(True)
        self.service.state.update_range(100)
        app = FastAPI()
        app.include_router(self.service.router)
        self.client = TestClient(app)

    def test_socket_drive_release_disconnect_and_single_owner(self):
        with self.client.websocket_connect("/ws/control") as ws:
            status = ws.receive_json()
            self.assertTrue(status["simulated"])
            self.assertTrue(status["requires_release"])
            with self.assertRaises(WebSocketDisconnect):
                with self.client.websocket_connect("/ws/control") as other:
                    other.receive_json()
            ws.send_json({"session_id": status["session_id"], "seq": 0, "deadman": False})
            ws.send_json({"session_id": status["session_id"], "permit": status["permit"],
                          "seq": 1, "deadman": True, "v": 0.5, "w": 0,
                          "video_fresh": True, "video_age_ms": 50})
            active = ws.receive_json()
            self.assertEqual(active["left_pwm"], 50)
        self.assertEqual(self.service.state.output()[:2], (0, 0))
        self.assertIsNone(self.service.state.session_id)

    def test_malformed_json_stops_and_public_status_cannot_issue_permit(self):
        with self.client.websocket_connect("/ws/control") as ws:
            ws.receive_json()
            ws.send_text("not json")
            status = ws.receive_json()
            self.assertEqual(status["reason"], "invalid_json")
            public = self.client.get("/control/status").json()
            self.assertNotIn("session_id", public)
            self.assertIsNone(public["permit"])


if __name__ == "__main__":
    unittest.main()
