import math
import time
import unittest

from fastapi.testclient import TestClient

from jetson.mapping.server import create_app
from jetson.sim.stream import SimFeed
from jetson.sim.world import Block, RoverSim, World
from jetson.sim.worlds import WORLDS, load


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def run(sim, clock, v, w, seconds):
    sim.apply(v, w)
    for _ in range(round(seconds / 0.05)):  # the control pump applies at 20 Hz
        clock.now += 0.05
        sim.advance()


class RoverSimTests(unittest.TestCase):
    def test_drive_turn_and_wall_collision(self):
        clock = Clock()
        sim = RoverSim(World("box", [Block(-2, -1.0, 2, -0.9)]), clock=clock)
        run(sim, clock, 1.0, 0.0, 1.0)
        self.assertAlmostEqual(sim.z, -0.45, places=6)  # forward is -Z at 0.45 m/s
        self.assertAlmostEqual(sim.x, 0.0, places=6)
        run(sim, clock, 0.0, 0.5, 0.5)  # positive w turns right (clockwise from above)
        self.assertAlmostEqual(sim.yaw, -0.75, places=6)
        sim.yaw = 0.0
        run(sim, clock, 1.0, 0.0, 1.0)  # into the wall at z = -0.9
        self.assertGreater(sim.collisions, 0)
        self.assertGreaterEqual(sim.clearance(sim.x, sim.z), RoverSim.RADIUS - 1e-9)

    def test_popup_ultrasonic_and_people(self):
        clock = Clock()
        sim = RoverSim(load("popup"), clock=clock)
        self.assertIsNone(sim.ultrasonic_cm())  # open corridor: no echo inside the Uno's 206 cm window
        box = next(b for b in sim.world.blocks if b.name == "popup box")
        self.assertFalse(box.visible)
        run(sim, clock, 1.0, 0.0, 4.0)  # 1.8 m: within 1.75 m of the box centre
        self.assertTrue(box.visible)
        self.assertEqual(sim.appeared[0]["name"], "popup box")
        self.assertLess(sim.ultrasonic_cm(), 206)
        seek = RoverSim(load("seek"))
        depth, _, pose = seek.render()
        boxes = seek.person_boxes(depth, pose)
        self.assertEqual([b["id"] for b in boxes], [0])
        self.assertTrue(0 <= boxes[0]["x"] <= 1 and boxes[0]["simulated"])

    def test_every_world_starts_clear_and_renders_depth(self):
        for name in WORLDS:
            with self.subTest(name=name):
                sim = RoverSim(load(name))
                self.assertGreater(sim.clearance(0, 0), RoverSim.RADIUS)
                depth, rgb, _ = sim.render()
                self.assertEqual(depth.shape, (120, 160))
                self.assertGreater((depth > 0).mean(), 0.5)
                self.assertGreater(sim.coverage(), 0)

    def test_feed_detections_match_perception_snapshot_shape(self):
        feed = SimFeed(RoverSim(load("seek")))
        self.assertFalse(feed.detections()["fresh"])
        frame = feed.frame(time.monotonic())
        self.assertEqual(frame.depth.shape, (120, 160))
        snapshot = feed.detections()
        self.assertTrue(snapshot["fresh"])
        self.assertEqual(snapshot["boxes"][0]["cls"], "person")


class SimServerTests(unittest.TestCase):
    def test_sim_world_is_opt_in_and_reports_ground_truth(self):
        with TestClient(create_app(detector="none")) as client:
            self.assertNotIn("sim_world", client.get("/health").json())
            self.assertEqual(client.get("/sim/state").status_code, 404)
        with TestClient(create_app(detector="none", sim_world="doorway")) as client:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not client.get("/health").json()["camera"]:
                time.sleep(0.05)
            health = client.get("/health").json()
            self.assertEqual((health["sim_world"], health["source"]), ("doorway", "SIMULATED"))
            self.assertTrue(health["camera"])
            state = client.get("/sim/state").json()
            self.assertEqual((state["world"], state["collisions"]), ("doorway", 0))
            self.assertTrue(math.isfinite(state["min_clearance_m"]))
        with self.assertRaises(ValueError):
            create_app(sim_world="popup", serial_port="/dev/null")


if __name__ == "__main__":
    unittest.main()
