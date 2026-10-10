import math
import concurrent.futures
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from jetson.mapping.server import create_app
from jetson.mapping.state import MapState
from jetson.nav import NavService
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
        state = MapState(demo=True, fusion_bypass=True)
        self.addCleanup(state.dense_worker.close)
        state.frame_observers.append(feed.offer_frame)
        state.process(frame)
        snapshot = feed.detections()
        self.assertTrue(snapshot["fresh"])
        self.assertEqual(snapshot["boxes"][0]["cls"], "person")

    def test_feed_pairs_seek_with_its_exact_accepted_capture(self):
        state = MapState(demo=True, fusion_bypass=True)
        self.addCleanup(state.dense_worker.close)
        nav = NavService(state)
        state.frame_observers.append(nav.offer_frame)
        feed = SimFeed(RoverSim(load('seek')))
        state.frame_observers.append(feed.offer_frame)
        frame = feed.frame(time.monotonic())
        state.process(frame)
        feed.header(time.monotonic(), frame.transform)  # Pose heartbeat advances a different frame ID.
        packet = feed.detections()
        self.assertEqual(packet.get('sensor_key'), frame.header['_mapping_sensor_key'])
        self.assertEqual(packet.get('frame'), frame.header['frame_id'])
        self.assertEqual(packet.get('source'), 'SIMULATED')
        self.assertIs(packet.get('mirrored'), False)
        nav.mode, nav.active, nav.target_id = 'seek', True, packet['boxes'][0]['id']
        nav.update_seek(packet)
        self.assertEqual(nav.seek['state'], 'driving')
        self.assertIsNotNone(nav.seek['goal'])

    def test_unaccepted_capture_is_not_fresh_or_given_a_guessed_key(self):
        feed = SimFeed(RoverSim(load('seek')))
        frame = feed.frame(time.monotonic())
        packet = feed.detections()
        self.assertFalse(packet['fresh'])
        self.assertEqual(packet['boxes'], [])
        self.assertIsNone(packet.get('sensor_key'))
        self.assertEqual(packet.get('frame'), -1)

    def test_pending_render_keeps_the_previous_accepted_packet_until_publication(self):
        state = MapState(demo=True, fusion_bypass=True)
        self.addCleanup(state.dense_worker.close)
        feed = SimFeed(RoverSim(load('seek')))
        state.frame_observers.append(feed.offer_frame)
        first = feed.frame(time.monotonic())
        state.process(first)
        previous = feed.detections()
        self.assertTrue(previous['fresh'])
        second = feed.frame(time.monotonic())
        pending = feed.detections()
        self.assertTrue(pending['fresh'], 'A pending render withdrew a valid accepted capture')
        self.assertEqual(pending['sensor_key'], previous['sensor_key'])
        self.assertEqual(pending['frame'], first.header['frame_id'])
        with patch('jetson.sim.stream.time.monotonic', return_value=first.header['_received_monotonic'] + 2):
            stale = feed.detections()
        self.assertFalse(stale['fresh'], 'An unfinished render renewed the previous capture age')
        self.assertEqual(stale['boxes'], [])
        self.assertEqual(stale['sensor_key'], previous['sensor_key'])
        state.process(second)
        published = feed.detections()
        self.assertEqual(published['sensor_key'], second.header['_mapping_sensor_key'])
        self.assertEqual(published['frame'], second.header['frame_id'])

    def test_bound_packet_waits_until_navigation_has_received_its_depth(self):
        state = MapState(demo=True, fusion_bypass=True)
        self.addCleanup(state.dense_worker.close)
        nav = NavService(state)
        feed = SimFeed(RoverSim(load('seek')))
        entered, release = threading.Event(), threading.Event()

        def delayed(frame):
            if frame.header['frame_id'] == 2:
                entered.set()
                release.wait(2)

        state.frame_observers.extend((delayed, nav.offer_frame, feed.offer_frame))
        first = feed.frame(time.monotonic())
        state.process(first)
        previous = feed.detections()
        second = feed.frame(time.monotonic())
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            job = pool.submit(state.process, second)
            try:
                self.assertTrue(entered.wait(1))
                self.assertIn('_mapping_sensor_key', second.header)
                packet = feed.detections()
                self.assertEqual(packet['sensor_key'], previous['sensor_key'],
                                 'Capture stamp was published before the navigation observer received it')
                with nav.lock:
                    self.assertIsNotNone(nav._seek_capture(packet, time.monotonic()))
            finally:
                release.set()
                job.result(timeout=1)
        self.assertEqual(feed.detections()['sensor_key'], second.header['_mapping_sensor_key'])

    def test_feed_retains_old_capture_key_across_reset_and_expires_its_boxes(self):
        state = MapState(demo=True, fusion_bypass=True)
        self.addCleanup(state.dense_worker.close)
        nav = NavService(state)
        state.frame_observers.append(nav.offer_frame)
        feed = SimFeed(RoverSim(load('seek')))
        state.frame_observers.append(feed.offer_frame)
        received = time.monotonic()
        frame = feed.frame(received)
        state.process(frame)
        original_key = frame.header['_mapping_sensor_key']
        state.start_session(feed.identity, 'Restarted simulated sensor')
        packet = feed.detections()
        self.assertEqual(packet.get('sensor_key'), original_key, 'Reset relabelled old boxes as a new capture')
        self.assertNotEqual(packet['sensor_key'][2], state.socket_generation)
        nav.sync_generation()
        with nav.lock:
            self.assertIsNone(nav._seek_capture(packet, time.monotonic()))
        with patch('jetson.sim.stream.time.monotonic', return_value=received + 2):
            expired = feed.detections()
        self.assertFalse(expired['fresh'])
        self.assertEqual(expired['boxes'], [])
        self.assertEqual(expired['sensor_key'], original_key)


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
