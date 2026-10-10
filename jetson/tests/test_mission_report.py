import json
from pathlib import Path
import tempfile
import time
import tracemalloc
import unittest

import cv2
import numpy as np

from jetson.camera import CameraFrame
from jetson.control.state import ControlState
from jetson.mission import ControlTelemetry, Mission


def frame(frame_id=1, source="USB CAMERA"):
    _, jpeg = cv2.imencode(".jpg", np.zeros((40, 60, 3), np.uint8))
    return CameraFrame(jpeg.tobytes(), time.monotonic(), frame_id, source)


def box(track, cls="person"):
    return dict(id=track, cls=cls, conf=.9, x=.4, y=.2, w=.2, h=.6)


def packet(range_cm=100, estop=False, reason="active", rover=True, session="s", requires_release=False):
    return {"type": "control_status", "session_id": session, "range_cm": range_cm, "estop": estop,
            "reason": reason, "rover": rover, "requires_release": requires_release}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class MissionReportTests(unittest.TestCase):
    def test_scripted_drive_estop_and_release_summarise(self):
        with tempfile.TemporaryDirectory() as directory:
            mission = Mission(directory)
            mission.observe_control(packet(range_cm=120))
            mission.detect(dict(boxes=[box(1)]), frame(1))
            mission.observe_control(packet(range_cm=18, estop=True, reason="forward_blocked"))
            mission.observe_control(packet(range_cm=18, estop=True, reason="forward_blocked"))
            mission.observe_control(packet(range_cm=60, reason="deadman_released"))
            mission.observe_control(packet(range_cm=60, rover=False, reason="serial_disconnected"))
            mission.observe_control(packet(range_cm=60, reason="release_required"))
            mission.observe_control(packet(range_cm=60), camera=True)
            mission.observe_control(packet(range_cm=60), camera=False)
            mission.detect(dict(boxes=[box(1), box(2, "dog")]), frame(2))
            report = mission.report()

            summary = report["summary"]
            self.assertEqual(summary["min_range_cm"], 18)
            self.assertEqual(summary["estop_count"], 1)
            self.assertEqual(summary["stop_reasons"]["forward_blocked"]["count"], 1)
            self.assertLessEqual(summary["stop_reasons"]["forward_blocked"]["first_ts"],
                                 summary["stop_reasons"]["forward_blocked"]["last_ts"])
            self.assertEqual(summary["lost"]["rover_link"]["count"], 1)
            self.assertIsNotNone(summary["lost"]["rover_link"]["intervals"][0]["end"])
            self.assertEqual(summary["lost"]["camera"]["count"], 1)
            self.assertIsNone(summary["lost"]["camera"]["intervals"][0]["end"])
            self.assertEqual(summary["detections_by_class"], {"person": 1, "dog": 1})

            person = next(e for e in report["encounters"] if e["id"] == 1)
            self.assertEqual(person["first_range_cm"], 120)  # range when first seen, not latest
            self.assertLessEqual(person["first_seen"], person["last_seen"])
            kinds = [event["kind"] for event in report["timeline"]]
            for kind in ("detection", "estop", "stop", "rover_link_lost", "rover_link_restored", "camera_lost"):
                self.assertIn(kind, kinds)

            persisted = [json.loads(line) for line in (Path(mission.directory) / "events.jsonl").read_text().splitlines()]
            self.assertIn("estop", [event["kind"] for event in persisted])
            self.assertIn("rover_link_lost", [event["kind"] for event in persisted])

    def test_real_control_status_packets_drive_the_summary(self):
        clock = Clock()
        state = ControlState(simulated=True, clock=clock, proximity_stop=True)
        mission = Mission()
        state.set_connected(True)
        session = state.begin()
        state.update_range(18)
        mission.observe_control(state.status())
        self.assertTrue(state.accept(dict(session_id=session, seq=0, deadman=False), session))
        mission.observe_control(state.status())
        summary = mission.report()["summary"]
        self.assertEqual(summary["min_range_cm"], 18)
        self.assertEqual(summary["estop_count"], 1)
        self.assertIn("deadman_released", summary["stop_reasons"])

    def test_startup_without_a_link_is_not_a_loss(self):
        mission = Mission()
        mission.observe_control(packet(rover=False, session=None, reason="rover_disabled"), camera=False)
        self.assertEqual({k: v["count"] for k, v in mission.report()["summary"]["lost"].items()},
                         dict(rover_link=0, controller=0, camera=0))

    def test_memory_stays_bounded_after_ten_thousand_packets(self):
        mission = Mission()
        reasons = ["forward_blocked", "deadman_released", "lease_expired", "active"]
        for i in range(200):
            mission.observe_control(packet(range_cm=50 + i % 7, estop=i % 3 == 0,
                                           reason=reasons[i % 4], rover=i % 5 != 0), camera=i % 9 != 0)
        tracemalloc.start()
        before = tracemalloc.take_snapshot()
        for i in range(10_000):
            mission.observe_control(packet(range_cm=18 + i % 300, estop=i % 3 == 0,
                                           reason=reasons[i % 4] if i % 11 else f"odd_{i}",
                                           rover=i % 5 != 0), camera=i % 9 != 0)
        growth = sum(stat.size_diff for stat in tracemalloc.take_snapshot().compare_to(before, "filename"))
        tracemalloc.stop()
        report = mission.report()
        self.assertEqual(report["summary"]["samples"], 10_200)
        self.assertLessEqual(len(report["timeline"]), ControlTelemetry.TIMELINE)
        self.assertLessEqual(len(report["summary"]["stop_reasons"]), ControlTelemetry.REASONS)
        for lost in report["summary"]["lost"].values():
            self.assertLessEqual(len(lost["intervals"]), ControlTelemetry.INTERVALS + 1)
        self.assertLess(growth, 512 * 1024)
        self.assertLess(len(json.dumps(report)), 512 * 1024)

    def test_server_samples_control_status_without_a_controller(self):
        from fastapi.testclient import TestClient
        from jetson.mapping.server import create_app
        app = create_app(detector="none", simulate_rover=True)
        with TestClient(app) as client:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                summary = client.get("/mission/report").json()["summary"]
                if summary["samples"] >= 2:
                    break
                time.sleep(.05)
            self.assertGreaterEqual(summary["samples"], 2)
            self.assertIsNone(client.get("/control/status").json()["permit"])


if __name__ == "__main__":
    unittest.main()
