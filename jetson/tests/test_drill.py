import math
import types
import unittest

from jetson.drill import Drill
from jetson.mission import Mission


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def face(id, **extra):
    return dict(id=id, cls="face", conf=.9, x=.4, y=.3, w=.1, h=.1, **extra)


class DrillTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.drill = Drill(clock=self.clock)

    def drive(self, v, w, seconds, blocked=False):
        """Apply a command at the control loop's 20 Hz for `seconds`."""
        for _ in range(round(seconds / .05)):
            self.drill.motion(v, w, blocked=blocked)
            self.clock.now += .05
        self.drill.motion(0, 0, blocked=False)

    def see(self, boxes, at):
        self.clock.now = at
        self.drill.observe(boxes)

    def test_face_counts_only_after_one_second_continuous_view(self):
        self.drill.start(90, victims=3)
        for t in (100.0, 100.3, 100.6, 100.9):
            self.see([face(4)], t)
        self.assertEqual(self.drill.status()["found"], 0)
        self.see([face(4)], 101.0)
        status = self.drill.status()
        self.assertEqual(status["found"], 1)
        self.assertEqual(status["total"], 3)
        self.assertEqual(status["found_ids"], [4])
        self.see([face(4)], 102.0)
        self.assertEqual(self.drill.status()["found"], 1)

    def test_persons_and_untracked_boxes_never_count(self):
        self.drill.start(90)
        for t in (100.0, 100.5, 101.2):
            self.see([dict(face(1), cls="person"), dict(face(None))], t)
        self.assertEqual(self.drill.status()["found"], 0)

    def test_short_detector_dropout_keeps_dwell_but_long_gap_restarts(self):
        self.drill.start(90)
        self.see([face(7)], 100.0)
        self.see([], 100.4)  # one missed frame
        self.see([face(7)], 100.6)
        self.see([face(7)], 101.0)
        self.assertEqual(self.drill.status()["found"], 1)

        self.see([face(8)], 102.0)
        self.see([face(8)], 102.5)
        self.see([face(8)], 104.0)  # lost for 1.5 s: dwell restarts here
        self.see([face(8)], 104.5)
        self.assertEqual(self.drill.status()["found"], 1)
        self.see([face(8)], 105.0)
        self.assertEqual(self.drill.status()["found_ids"], [7, 8])

    def test_id_flicker_between_tracks_does_not_count_either(self):
        self.drill.start(90)
        # The tracker alternates ids on one face: each id misses every other frame.
        for index in range(8):
            self.see([face(10 + index % 2)], 100.0 + index * .2)
        self.assertEqual(self.drill.status()["found"], 0)

    def test_faces_before_start_or_after_timeout_do_not_count(self):
        self.see([face(1)], 99.0)
        self.see([face(1)], 100.5)
        self.assertEqual(self.drill.status()["found"], 0)
        self.drill.start(10)
        self.see([face(2)], 109.5)
        self.see([face(2)], 111.0)
        status = self.drill.status()
        self.assertFalse(status["active"])
        self.assertEqual(status["found"], 0)

    def test_estop_counts_each_blocked_forward_attempt_once(self):
        self.drill.start(90)
        self.clock.now = 100.0
        self.drill.motion(.5, 0, blocked=False)
        for t in (100.1, 100.15, 100.2):
            self.clock.now = t
            self.drill.motion(0, 0, blocked=True)
        self.assertEqual(self.drill.status()["estops"], 1)
        self.clock.now = 101.0
        self.drill.motion(.5, 0, blocked=False)
        self.clock.now = 101.1
        self.drill.motion(0, 0, blocked=True)
        self.assertEqual(self.drill.status()["estops"], 2)
        self.clock.now = 101.15  # chattering sensor right after a block is the same event
        self.drill.motion(.5, 0, blocked=False)
        self.clock.now = 101.2
        self.drill.motion(0, 0, blocked=True)
        self.assertEqual(self.drill.status()["estops"], 2)

    def test_estops_outside_drill_are_ignored(self):
        self.drill.motion(0, 0, blocked=True)
        self.assertEqual(self.drill.status()["estops"], 0)

    def test_dead_reckoning_straight_then_turn(self):
        self.drill.start(90)
        speed, turn = Drill.MAX_SPEED_MPS, Drill.MAX_TURN_RADPS
        self.drive(1.0, 0, 2.0)
        pose = self.drill.status()["pose"]
        self.assertAlmostEqual(pose["x"], 2 * speed, places=2)
        self.assertAlmostEqual(pose["y"], 0, places=3)
        # Positive w turns right (clockwise): heading decreases.
        self.drive(0, 1.0, (math.pi / 2) / turn)
        self.assertAlmostEqual(self.drill.status()["pose"]["heading_rad"], -math.pi / 2, places=1)
        self.drive(1.0, 0, 1.0)
        pose = self.drill.status()["pose"]
        self.assertAlmostEqual(pose["x"], 2 * speed, delta=.03)
        self.assertAlmostEqual(pose["y"], -speed, delta=.03)

    def test_breadcrumb_is_timestamped_and_route_home_reverses_it(self):
        self.drill.start(90)
        self.drive(1.0, 0, 5.0)
        status = self.drill.status()
        crumbs = status["breadcrumb"]
        self.assertEqual(crumbs[0], dict(t=0.0, x=0.0, y=0.0))
        self.assertGreater(len(crumbs), 10)
        self.assertTrue(all(b["t"] > a["t"] for a, b in zip(crumbs, crumbs[1:])))
        self.assertAlmostEqual(crumbs[-1]["x"], 5 * Drill.MAX_SPEED_MPS, places=2)
        home = status["route_home"]
        self.assertEqual(home[0], dict(x=crumbs[-1]["x"], y=crumbs[-1]["y"]))
        self.assertEqual(home[-1], dict(x=0.0, y=0.0))
        self.assertTrue(status["route_approximate"])

    def test_long_gaps_between_outputs_do_not_extrapolate_motion(self):
        self.drill.start(90)
        self.clock.now = 100.0
        self.drill.motion(1.0, 0, blocked=False)
        self.clock.now = 110.0  # control loop stalled: no outputs for 10 s
        self.drill.motion(1.0, 0, blocked=False)
        self.assertLessEqual(self.drill.status()["pose"]["x"], Drill.MAX_DT * Drill.MAX_SPEED_MPS + 1e-6)

    def test_breadcrumb_stays_bounded_on_long_drives(self):
        self.drill.start(300)
        self.drive(1.0, .3, 280)
        crumbs = self.drill.status()["breadcrumb"]
        self.assertLessEqual(len(crumbs), Drill.MAX_CRUMBS + 1)
        self.assertEqual(crumbs[0], dict(t=0.0, x=0.0, y=0.0))

    def test_end_summary_after_timeout(self):
        self.drill.start(10, victims=2)
        for t in (100.0, 100.5, 101.0, 101.5):
            self.see([face(3)], t)
        self.clock.now = 102.0
        self.drill.motion(.5, 0, blocked=False)
        self.clock.now = 102.1
        self.drill.motion(0, 0, blocked=True)
        self.clock.now = 115.0
        status = self.drill.status()
        self.assertFalse(status["active"])
        self.assertEqual(status["remaining_s"], 0)
        summary = status["summary"]
        self.assertEqual(summary["time_s"], 10)
        self.assertEqual(summary["found"], 1)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["estops_avoided"], 1)
        self.assertEqual(summary["found_at_s"], [1.0])
        self.assertIn("breadcrumb", summary)

    def test_finding_every_victim_ends_the_drill_early(self):
        self.drill.start(90, victims=1)
        for t in (100.0, 100.6, 101.2):
            self.see([face(5)], t)
        self.clock.now = 130.0
        status = self.drill.status()
        self.assertFalse(status["active"])
        self.assertEqual(status["summary"]["time_s"], 1.2)
        self.assertEqual(status["summary"]["found"], 1)

    def test_summary_is_none_while_running_and_restart_resets(self):
        self.drill.start(90)
        self.assertIsNone(self.drill.status()["summary"])
        for t in (100.0, 100.5, 101.0):
            self.see([face(1)], t)
        self.assertEqual(self.drill.status()["found"], 1)
        self.clock.now = 102.0
        self.drill.start(90)
        status = self.drill.status()
        self.assertEqual(status["found"], 0)
        self.assertEqual(status["breadcrumb"], [dict(t=0.0, x=0.0, y=0.0)])

    def test_not_started_status_is_wire_compatible(self):
        status = self.drill.status()
        self.assertFalse(status["active"])
        self.assertIsNone(status["remaining_s"])
        self.assertEqual(status["found"], 0)


class MissionDrillTests(unittest.TestCase):
    def test_mission_feeds_face_tracks_and_reports_end_summary(self):
        clock = Clock()
        mission = Mission(drill_clock=clock)
        frame = types.SimpleNamespace(source="SIMULATED", frame_id=0, jpeg=b"")
        status = mission.start_drill(10, victims=2)
        self.assertEqual((status["active"], status["total"], status["found"]), (True, 2, 0))
        for t in (100.0, 100.5, 101.0):
            clock.now = t
            mission.detect({"boxes": [face(1), dict(face(2), cls="person")]}, frame)
        mission.drill_motion(0.5, 0, True)
        clock.now = 111.0
        drill = mission.report()["drill"]
        self.assertEqual(drill["found"], 1)
        self.assertEqual(drill["person_encounters"], 1)
        self.assertEqual(drill["summary"]["estops_avoided"], 1)
        self.assertEqual(drill["summary"]["total"], 2)


class DrillServerTests(unittest.TestCase):
    def test_drill_routes_and_control_hook(self):
        from fastapi.testclient import TestClient
        from jetson.mapping.server import create_app
        app = create_app(detector="none", simulate_rover=True)
        with TestClient(app) as client:
            self.assertEqual(app.state.control.state.on_output, app.state.mission.drill_motion)
            self.assertEqual(client.post("/mission/drill", json={"duration_s": 90, "victims": 0}).status_code, 422)
            started = client.post("/mission/drill", json={"duration_s": 30, "victims": 4}).json()
            self.assertEqual((started["active"], started["total"], started["duration_s"]), (True, 4, 30))
            status = client.get("/mission/drill").json()
            for field in ("breadcrumb", "route_home", "estops", "found_ids", "summary", "person_encounters"):
                self.assertIn(field, status)
            self.assertTrue(status["route_approximate"])


if __name__ == "__main__":
    unittest.main()
