"""Camera-only scan geometry, control safety, and existing map delivery."""

import unittest
import time
import struct
import threading
import os
import numpy as np
from fastapi.testclient import TestClient

from jetson.depth import back_project, intrinsics_from_fov, MetricDepth
from jetson.mapping.voxel import VoxelMap
from jetson.control.state import ControlState


class LatestControlStatus:
    """Drain telemetry independently of HTTP/map work, as the real client does."""
    def __init__(self, ws):
        self.ws = ws
        self.condition = threading.Condition()
        self.status = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.receive, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.latest()
        return self

    def receive(self):
        while not self.stopped.is_set():
            try:
                status = self.ws.receive_json()
            except Exception:
                return
            with self.condition:
                self.status = status
                self.condition.notify_all()

    def latest(self, predicate=lambda value: True, timeout=2):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.status is None or not predicate(self.status):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"Control telemetry condition timed out: {self.status}")
                self.condition.wait(remaining)
            return dict(self.status)

    def __exit__(self, *args):
        self.stopped.set()
        self.thread.join(timeout=.3)


class ScanGeometryTests(unittest.TestCase):
    def test_wall_at_two_metres_and_right_yaw_use_viewer_negative_z(self):
        depth = np.full((3, 3), 2, np.float32)
        rgb = np.full((3, 3, 3), [12, 34, 56], np.uint8)
        k = intrinsics_from_fov(3, 3, 90)
        points, colors = back_project(depth, rgb, k, 0)
        np.testing.assert_allclose(points[:, 2], -2)
        np.testing.assert_allclose(points[4], [0, 0, -2], atol=1e-6)
        np.testing.assert_array_equal(colors[4], [12, 34, 56])
        turned, _ = back_project(depth, rgb, k, 90)
        np.testing.assert_allclose(turned[4], [2, 0, 0], atol=1e-6)
        self.assertGreater(points[0, 1], 0)  # top pixels are above the camera

    def test_voxel_merge_averages_overlap_and_bounds_scan(self):
        cloud = VoxelMap(size=.04, capacity=50_000)
        cloud.integrate(np.array([[0, 0, -2], [.01, 0, -2]]),
                        np.array([[0, 0, 0], [100, 200, 100]], np.uint8))
        xyz, rgb = cloud.snapshot()
        np.testing.assert_allclose(xyz, [[.005, 0, -2]])
        np.testing.assert_array_equal(rgb, [[50, 100, 50]])
        xyz = np.column_stack((np.arange(60_000) * .05, np.zeros(60_000), np.zeros(60_000)))
        cloud.integrate(xyz, np.zeros((60_000, 3), np.uint8))
        self.assertEqual(len(cloud.snapshot()[0]), 50_000)

    def test_invalid_depth_is_excluded(self):
        d = np.array([[np.nan, np.inf, 0, .15, 9, 2]], np.float32)
        xyz, _ = back_project(d, np.zeros((1, 6, 3), np.uint8), (2, 2, 5, 0), 0)
        np.testing.assert_allclose(xyz, [[0, 0, -2]])


class DepthModelTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("FARSIGHT_DEPTH_MODEL"), "Set FARSIGHT_DEPTH_MODEL for real ONNX/TensorRT smoke")
    def test_real_depth_model_on_demo_rgb(self):
        import cv2
        from jetson.mapping.demo import simulated_frame
        frame = simulated_frame(0, 0, scan_yaw=0)
        model = MetricDepth(os.environ["FARSIGHT_DEPTH_MODEL"])
        try:
            depth = model.predict(cv2.cvtColor(frame.rgb, cv2.COLOR_RGB2BGR))
            self.assertEqual(depth.shape, (120, 160))
            self.assertTrue(np.isfinite(depth).all())
            self.assertGreater(float(np.median(depth)), .15)
        finally:
            model.close()

    def test_rgb_imagenet_normalization_and_output_stay_metric(self):
        class Session:
            def get_inputs(self):
                from types import SimpleNamespace
                return [SimpleNamespace(name="pixel_values", shape=[1, 3, 308, 406])]

            def run(inner, _, feed):
                blob = feed["pixel_values"]
                self.assertEqual(blob.shape, (1, 3, 308, 406))
                np.testing.assert_allclose(blob[0, :, 100, 100],
                    [(1 - .485) / .229, -.456 / .224, -.406 / .225], rtol=1e-5)
                return [np.full((1, 308, 406), 2.5, np.float32)]

        bgr = np.zeros((12, 16, 3), np.uint8)
        bgr[:, :, 2] = 255
        depth = MetricDepth(session=Session()).predict(bgr)
        self.assertEqual(depth.shape, (12, 16))
        np.testing.assert_allclose(depth, 2.5, atol=1e-5)


class ScanSafetyTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.state = ControlState(simulated=True, clock=lambda: self.now, proximity_stop=True)
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.seq = 0
        self.send(deadman=False)
        self.send()

    def send(self, **values):
        status = self.state.status(issue_permit=True)
        self.permit = status["permit"]
        self.seq += 1
        self.state.update_range(120)
        message = dict(session_id=self.session, permit=self.permit, seq=self.seq,
                       deadman=True, mode="teleop", v=0, w=0, pan=0,
                       video_fresh=True, video_age_ms=0)
        message.update(values)
        return self.state.accept(message, self.session)

    def test_refuses_moving_and_missing_or_wrong_operator_permit(self):
        for session, permit in ((self.session, None), ("other", self.permit)):
            with self.assertRaises(ValueError):
                self.state.begin_scan(session, permit)
        self.send(w=.3)
        with self.assertRaises(ValueError):
            self.state.begin_scan(self.session, self.permit)

    def test_scan_uses_held_lease_and_release_expiry_camera_or_range_abort(self):
        for failure in ("release", "expiry", "camera", "range", "disconnect", "override"):
            with self.subTest(failure=failure):
                self.setUp()
                token = self.state.begin_scan(self.session, self.permit)
                self.state.scan_command(token, w=.3)
                self.assertGreater(self.state.output()[0], 0)
                self.now += .1
                self.assertTrue(self.send(mode="scan"))
                self.assertGreater(self.state.output()[0], 0)
                if failure == "release":
                    self.send(deadman=False)
                elif failure == "expiry":
                    self.now += .251
                elif failure == "camera":
                    self.state.camera_ready = lambda: False
                elif failure == "range":
                    self.state.update_range(20)
                elif failure == "disconnect":
                    self.state.end(self.session)
                else:
                    self.send(w=0)
                self.assertEqual(self.state.output()[:2], (0, 0))
                with self.assertRaises(ValueError):
                    self.state.scan_command(token, w=.3)

    def test_scan_cancel_reports_first_stop_after_later_release(self):
        for failure, reason in (("expiry", "lease_expired"), ("camera", "camera_stale"),
                                ("range", "scan_range_stop"), ("override", "scan_override"),
                                ("release", "deadman_released"),
                                ("disconnect", "controller_disconnected")):
            with self.subTest(failure=failure):
                self.setUp()
                token = self.state.begin_scan(self.session, self.permit)
                self.state.scan_command(token, w=.3)
                if failure == "expiry":
                    self.now += .251
                elif failure == "camera":
                    self.state.camera_ready = lambda: False
                elif failure == "range":
                    self.state.update_range(20)
                elif failure == "override":
                    self.send()
                elif failure == "release":
                    self.send(deadman=False)
                else:
                    self.state.end(self.session)
                self.state.output()
                # The viewer responds to a safety stop with an immediate release.
                # That later status must not erase the scan's initiating cause.
                self.state.stop("deadman_released")
                self.state.stop("release_required")
                with self.assertRaisesRegex(ValueError, rf"Scan cancelled:.*{reason}"):
                    self.state.scan_command(token)

    def test_new_scan_clears_previous_stop_reason(self):
        token = self.state.begin_scan(self.session, self.permit)
        self.send(deadman=False)
        self.send()
        fresh = self.state.begin_scan(self.session, self.permit)
        self.state.camera_ready = lambda: False
        with self.assertRaisesRegex(ValueError, r"Scan cancelled:.*camera_stale"):
            self.state.scan_command(fresh)
        self.assertIsNot(token, fresh)


class ScanDeliveryTests(unittest.TestCase):
    def test_markers_use_matching_capture_and_same_yaw_geometry(self):
        from jetson.mapping.server import create_app
        from jetson.camera import CameraFrame
        scanner = create_app(demo=True).state.scanner
        scanner.result = dict(markers=[])
        depth = np.full((3, 3), 2, np.float32)
        rgb = np.zeros((3, 3, 3), np.uint8)
        frame = CameraFrame(b"", time.monotonic(), 7, "USB CAMERA")
        packet = dict(fresh=True, error=None, source=frame.source, frame=7,
                      boxes=[dict(id=4, cls="person", x=1/3, y=1/3, w=1/3, h=1/3)])
        scanner.collect_markers(frame, depth, rgb, (2, 2, 1, 1), 90, dict(packet, frame=6))
        self.assertEqual(scanner.result["markers"], [])
        scanner.collect_markers(frame, depth, rgb, (2, 2, 1, 1), 90, packet)
        marker = scanner.result["markers"][0]
        self.assertEqual((marker["id"], marker["cls"]), (4, "person"))
        np.testing.assert_allclose(marker["position"], [2, 0, 0], atol=1e-6)

    def test_release_or_map_reset_during_depth_never_publishes_a_partial_scan(self):
        from jetson.mapping.server import create_app
        from types import SimpleNamespace
        for interrupt in ("release", "reset"):
            with self.subTest(interrupt=interrupt):
                entered, finish = threading.Event(), threading.Event()
                class Session:
                    def get_inputs(self):
                        return [SimpleNamespace(name="image", shape=[1, 3, 308, 406])]
                    def run(self, *_):
                        entered.set()
                        if not finish.wait(3):
                            raise RuntimeError("test inference timed out")
                        return [np.full((1, 308, 406), 2, np.float32)]
                app = create_app(demo=True, simulate_rover=True, detector="none")
                app.state.scanner.depth = MetricDepth(session=Session())
                with TestClient(app) as client, client.websocket_connect("/ws/control") as ws, LatestControlStatus(ws) as telemetry:
                    status = telemetry.latest(lambda value: value["rover"])
                    def send(seq, held, mode):
                        status = telemetry.latest()
                        ws.send_json(dict(session_id=status["session_id"], permit=status["permit"], seq=seq,
                            deadman=held, v=0, w=0, pan=0, mode=mode, video_fresh=True, video_age_ms=0))
                    send(1, False, "teleop")
                    telemetry.latest(lambda value: value["seq"] >= 1)
                    deadline = time.monotonic() + 2
                    while not client.get("/health").json()["camera"]:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.02)
                    send(2, True, "teleop")
                    telemetry.latest(lambda value: value["seq"] >= 2)
                    status = telemetry.latest()
                    self.assertEqual(client.post("/scan", json=dict(session_id=status["session_id"], permit=status["permit"])).status_code, 202)
                    self.assertTrue(entered.wait(1))
                    if interrupt == "reset":
                        client.post("/map/reset")
                    else:
                        send(3, False, "scan")
                    finish.set()
                    for seq in range(4, 35):
                        if interrupt == "reset":
                            send(seq, True, "scan")
                        result = client.get("/scan").json()
                        if not result["running"]:
                            break
                        time.sleep(.04)
                    self.assertEqual(result["state"], "aborted", result)
                    self.assertIsNone(app.state.mapping.scan)
                    self.assertEqual(app.state.control.state.output()[:2], (0, 0))

    def test_simulated_sweep_replaces_existing_map_and_survives_reset(self):
        from jetson.mapping.server import create_app
        app = create_app(demo=True, simulate_rover=True, detector="none")
        with TestClient(app) as client, client.websocket_connect("/ws/control") as ws, LatestControlStatus(ws) as telemetry:
            status = telemetry.latest(lambda value: value["rover"])
            seq = 0
            def heartbeat(held, mode="teleop"):
                nonlocal seq
                seq += 1
                status = telemetry.latest()
                ws.send_json(dict(session_id=status["session_id"], permit=status["permit"], seq=seq,
                    deadman=held, v=0, w=0, pan=0, mode=mode, video_fresh=True, video_age_ms=0))

            self.assertEqual(client.post("/scan", json={}).status_code, 422)
            heartbeat(False)
            telemetry.latest(lambda value: value["seq"] >= seq)
            deadline = time.monotonic() + 2
            while not client.get("/health").json()["camera"]:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            heartbeat(True)
            telemetry.latest(lambda value: value["seq"] >= seq)
            status = telemetry.latest()
            self.assertEqual(client.post("/scan", json=dict(session_id=status["session_id"], permit="invalid")).status_code, 409)
            response = client.post("/scan", json=dict(session_id=status["session_id"], permit=status["permit"]))
            self.assertEqual(response.status_code, 202, response.text)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                heartbeat(True, "scan")
                result = client.get("/scan").json()
                if not result["running"]:
                    break
                time.sleep(.04)
            self.assertEqual(result["state"], "complete", result)
            self.assertEqual(len(result["views"]), 5)
            self.assertTrue(result["simulated"])
            self.assertTrue(result["approximate"])
            packet = client.get("/map/points.bin").content
            count = result["points"]
            self.assertGreater(count, 1000)
            self.assertLessEqual(count, 100_000)
            sent = struct.unpack_from("<I", packet)[0]
            self.assertEqual(sent, min(count, 30_000))  # visual wire cap; the scan itself stays full
            self.assertEqual(len(packet), 4 + sent * 16)
            with client.websocket_connect("/ws/map") as map_ws:
                self.assertEqual(map_ws.receive_json()["scan"]["points"], count)
                self.assertEqual(map_ws.receive_bytes(), packet)
            health = client.get("/health").json()
            self.assertEqual(health["scan"]["points"], count)
            self.assertEqual(health["capped"], count > 30_000)
            self.assertTrue(health["paused"])
            self.assertEqual(health["triangles"], 0)
            heartbeat(False)
            resumed = client.post("/map/pause", json={"paused": False}).json()
            self.assertIsNone(resumed["scan"])
            self.assertEqual(resumed["voxel_m"], .04)
            client.post("/map/pause", json={"paused": True})
            client.post("/map/reset")
            self.assertEqual(struct.unpack_from("<I", client.get("/map/points.bin").content)[0], 0)


if __name__ == "__main__":
    unittest.main()
