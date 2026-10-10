"""Exercise the assembled application, not a substitute control-only router."""

import time
import unittest

import cv2
import numpy as np
from fastapi.testclient import TestClient

from jetson.mapping.demo import simulated_frame
from jetson.mapping.server import create_app


class FullServerTests(unittest.TestCase):
    def test_demo_explore_seek_target_loss_stick_override_and_link_loss(self):
        app = create_app(demo=True, simulate_rover=True, detector="none")
        perception = app.state.perception

        def detected(x=None):
            # Fake inference results, real camera, control socket, and serial pump.
            perception.packet = dict(ts=time.time(), frame=1, fps=10, detector="FAKE",
                                     source="SIMULATED", received=time.monotonic(),
                                     boxes=[] if x is None else [dict(id=7, cls="person", x=x, w=.2)])

        with TestClient(app) as client:
            self.wait_health(client, lambda value: value["camera"] and value["rover"])
            with client.websocket_connect("/ws/control") as ws:
                status = self.release(ws, ws.receive_json(), 0)
                detected()
                self.motion(ws, status, 1, mode="explore", v=0)
                status = self.wait_status(ws, lambda value: value["seq"] == 1)
                self.assertEqual((status["mode"], status["autonomy_state"]), ("explore", "forward"))
                self.assertEqual((status["left_pwm"], status["right_pwm"]), (25, 25))
                detected(.7)
                self.motion(ws, status, 2, mode="explore", v=0)
                status = self.wait_status(ws, lambda value: value["seq"] == 2)
                self.assertEqual((status["mode"], status["target_id"]), ("seek", 7))
                self.assertGreater(status["left_pwm"], status["right_pwm"])
                detected(.1)
                self.motion(ws, status, 3, mode="explore", v=0)
                status = self.wait_status(ws, lambda value: value["seq"] == 3)
                self.assertLess(status["left_pwm"], status["right_pwm"])
                detected()
                seq = 3
                deadline = time.monotonic() + 1.4
                while time.monotonic() < deadline and status["autonomy_state"] != "target_lost":
                    seq += 1
                    self.motion(ws, status, seq, mode="explore", v=0)
                    status = self.wait_status(ws, lambda value: value["seq"] == seq)
                    self.assertEqual((status["left_pwm"], status["right_pwm"]), (0, 0))
                self.assertEqual(status["autonomy_state"], "target_lost")
                detected(.4)
                self.motion(ws, status, seq + 1, mode="explore", v=-.5)
                status = self.wait_status(ws, lambda value: value["seq"] == seq + 1)
                self.assertEqual((status["mode"], status["autonomy_state"], status["left_pwm"]),
                                 ("teleop", "stick_override", -50))
                status = self.release(ws, status, seq + 2)
                self.motion(ws, status, seq + 3, mode="seek", target_id=7, v=0)
                status = self.wait_status(ws, lambda value: value["seq"] == seq + 3)
                self.assertGreater(status["left_pwm"], 0)
                stopped = self.wait_status(ws, lambda value: value["reason"] == "lease_expired")
                self.assertEqual((stopped["left_pwm"], stopped["right_pwm"]), (0, 0))
                self.assertTrue(stopped["requires_release"])
            self.assertEqual(app.state.control.state.output()[:2], (0, 0))

    def wait_health(self, client, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            status = client.get("/health").json()
            if predicate(status):
                return status
            if time.monotonic() > deadline:
                self.fail(f"Health condition timed out: {status}")
            time.sleep(0.02)

    def wait_status(self, ws, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while True:
            status = ws.receive_json()
            if predicate(status):
                return status
            if time.monotonic() > deadline:
                self.fail(f"Control condition timed out: {status}")

    def release(self, ws, status, seq):
        ws.send_json({"session_id": status["session_id"], "seq": seq, "deadman": False})
        return self.wait_status(ws, lambda value: value["seq"] == seq)

    def motion(self, ws, status, seq, **values):
        message = dict(session_id=status["session_id"], permit=status["permit"],
                       seq=seq, deadman=True, v=0.5, w=0, pan=0,
                       mode="teleop", video_fresh=True, video_age_ms=0)
        message.update(values)
        ws.send_json(message)

    def test_simulated_camera_stream_and_expired_permit_motor_stop(self):
        app = create_app(demo=True, simulate_rover=True, detector="off")
        with TestClient(app) as client:
            health = self.wait_health(client, lambda value: value["camera"] and value["rover"])
            self.assertEqual(health["camera_source"], "SIMULATED")
            self.assertTrue(health["control"]["simulated"])
            preview = client.get("/camera.jpg")
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.headers["cache-control"], "no-store")
            self.assertEqual(preview.headers["x-frame-source"], "SIMULATED")
            image = cv2.imdecode(np.frombuffer(preview.content, np.uint8), cv2.IMREAD_COLOR)
            self.assertIsNotNone(image)
            self.assertGreater(image.shape[0], 100)
            self.assertLess(int(preview.headers["x-frame-age-ms"]), 500)
            self.assertEqual(client.get("/map/points.bin").status_code, 200)
            with client.websocket_connect("/ws/control") as ws:
                status = ws.receive_json()
                status = self.release(ws, status, 0)
                self.motion(ws, status, 1)
                active = self.wait_status(ws, lambda value: value["seq"] == 1)
                self.assertEqual((active["left_pwm"], active["right_pwm"]), (50, 50))
                # Even a later sequence and an apparently fresh camera cannot
                # restore a motor command using an old network permit.
                old_permit = active["permit"]
                time.sleep(0.3)
                self.motion(ws, active, 2, permit=old_permit)
                stopped = self.wait_status(ws, lambda value: value["reason"] == "expired_permit")
                self.assertEqual((stopped["left_pwm"], stopped["right_pwm"]), (0, 0))
                self.assertTrue(stopped["requires_release"])
                self.assertEqual(stopped["seq"], 1)
            self.assertEqual(app.state.control.state.output()[:2], (0, 0))

    def test_demo_camera_and_permits_continue_while_fusion_is_blocked(self):
        app = create_app(demo=True, simulate_rover=True, detector="none")
        with TestClient(app) as client:
            self.wait_health(client, lambda value: value["camera"] and value["rover"])
            # Hold the actual fusion lock. Camera/control must progress without
            # waiting for it, through the same public routes as the operator.
            with app.state.mapping.lock:
                first = client.get("/camera.jpg")
                first_id = int(first.headers["x-frame-id"])
                deadline = time.monotonic() + 1
                while time.monotonic() < deadline:
                    current = client.get("/camera.jpg")
                    if int(current.headers["x-frame-id"]) >= first_id + 2:
                        break
                    time.sleep(.02)
                self.assertGreaterEqual(int(current.headers["x-frame-id"]), first_id + 2)
                self.assertEqual(current.headers["x-frame-source"], "SIMULATED")
                self.assertLess(int(current.headers["x-frame-age-ms"]), 500)
                with client.websocket_connect("/ws/control") as ws:
                    status = self.release(ws, ws.receive_json(), 0)
                    self.motion(ws, status, 1)
                    active = self.wait_status(ws, lambda value: value["seq"] == 1)
                    self.assertEqual((active["left_pwm"], active["right_pwm"]), (50, 50))

    def test_pose_heartbeat_does_not_hide_camera_outage_or_restore_motion(self):
        app = create_app(simulate_rover=True, detector="off")
        mapping = app.state.mapping
        identity = ("integration-phone", 1)
        mapping.start_session(identity, "Test RGB-D phone")
        frame = simulated_frame(0, 0, identity)
        mapping.process(frame)
        with TestClient(app) as client:
            self.wait_health(client, lambda value: value["camera"] and value["rover"])
            with client.websocket_connect("/ws/control") as ws:
                status = self.release(ws, ws.receive_json(), 0)
                self.motion(ws, status, 1, v=-0.5)  # guard also applies to reverse
                self.wait_status(ws, lambda value: value["left_pwm"] == -50)
                with mapping.lock:
                    mapping.last_frame_received = time.monotonic() - 1
                    jpeg, _, identifier, source = mapping.camera_snapshot()
                    mapping.latest_camera = (jpeg, mapping.last_frame_received, identifier, source)
                pose_header = dict(frame.header, t_capture=1, frame_id=100)
                mapping.observe_pose(pose_header, frame.transform)
                health = client.get("/health").json()
                self.assertTrue(health["fresh"])  # pose link genuinely still live
                self.assertFalse(health["camera"])
                stopped = self.wait_status(ws, lambda value: value["reason"] == "camera_stale")
                self.assertEqual((stopped["left_pwm"], stopped["right_pwm"]), (0, 0))
                self.assertTrue(stopped["requires_release"])
                mapping.process(simulated_frame(2, 1, identity))
                self.assertTrue(client.get("/health").json()["camera"])
                self.motion(ws, stopped, 2)
                # Fusion can outlive the stopped packet's 250 ms permit. Both
                # rejection paths must preserve the held-trigger release latch.
                refused = self.wait_status(ws, lambda value: value["requires_release"]
                                           and value["reason"] in ("release_required", "expired_permit"))
                self.assertEqual(refused["left_pwm"], 0)
                status = self.release(ws, refused, 3)
                self.motion(ws, status, 4)
                resumed = self.wait_status(ws, lambda value: value["seq"] == 4)
                self.assertEqual(resumed["left_pwm"], 50)

    def test_simulated_serial_disconnect_in_assembled_server_requires_new_release(self):
        app = create_app(demo=True, simulate_rover=True, detector="off")
        with TestClient(app) as client:
            self.wait_health(client, lambda value: value["camera"] and value["rover"])
            with client.websocket_connect("/ws/control") as ws:
                status = self.release(ws, ws.receive_json(), 0)
                self.motion(ws, status, 1)
                active = self.wait_status(ws, lambda value: value["seq"] == 1)
                app.state.control.state.set_connected(False)
                self.assertFalse(client.get("/health").json()["rover"])
                stopped = self.wait_status(ws, lambda value: not value["rover"])
                self.assertEqual(stopped["left_pwm"], 0)
                self.motion(ws, active, 2)
                self.wait_status(ws, lambda value: value["reason"] == "expired_permit")
                app.state.control.state.set_connected(True)
                app.state.control.state.update_range(120)
                reconnected = self.wait_status(ws, lambda value: value["rover"])
                self.motion(ws, reconnected, 3)
                refused = self.wait_status(ws, lambda value: value["reason"] == "release_required")
                self.assertEqual(refused["left_pwm"], 0)
                self.assertTrue(refused["requires_release"])

    def test_demo_mapping_never_silently_enables_motor_simulation(self):
        app = create_app(demo=True, detector="off")
        with TestClient(app) as client:
            health = self.wait_health(client, lambda value: value["camera"])
            self.assertFalse(health["rover"])
            self.assertFalse(health["control"]["simulated"])
            self.assertEqual(health["control"]["reason"], "rover_disabled")
            with client.websocket_connect("/ws/control") as ws:
                status = self.release(ws, ws.receive_json(), 0)
                self.motion(ws, status, 1)
                denied = self.wait_status(ws, lambda value: value["reason"] == "serial_disconnected")
                self.assertEqual((denied["left_pwm"], denied["right_pwm"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
