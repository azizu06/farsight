import unittest
import os
import threading
import time
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from jetson.mapping.dense import DenseMap, RECORD, unpack
from jetson.mapping.phone import measured_points
from jetson.mapping.server import create_app
from jetson.mapping.state import MapState
from jetson.mapping.dense_worker import DENSE_FRAME_POINTS, DenseWorker
from jetson.mapping.tests.test_mapping import flat_frame


class DenseTests(unittest.TestCase):
    def test_measured_native_pixels_rgb_pose_and_tracking_bypass(self):
        frame = flat_frame()
        frame.transform[:3, 3] = [1, 2, 3]
        xyz, rgb = measured_points(frame)
        self.assertEqual(len(xyz), frame.depth.size)
        np.testing.assert_allclose(xyz[0], [0, 2.75, 1])
        np.testing.assert_array_equal(rgb, frame.rgb.reshape(-1, 3))
        state = MapState(fusion_bypass=True)
        self.addCleanup(state.dense_worker.close)
        state.start_session(("test", 1), "test")
        state.process(frame)
        deadline = time.monotonic() + 2
        while not state.dense_worker.processed and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(len(state.cloud.cells), 0)
        self.assertEqual(len(state.dense.cells), frame.depth.size)
        previous = state.dense.revision
        frame.header.update(t_capture=2, tracking="limited")
        state.process(frame)
        self.assertEqual(state.dense.revision, previous)
        frame.header.update(t_capture=3, tracking="normal")
        frame.confidence[:] = 0
        state.process(frame)
        self.assertEqual(state.dense.revision, previous)
        epoch = state.dense.epoch
        state.start_session(("other", 2), "other")
        self.assertNotEqual(state.dense.epoch, epoch)
        self.assertFalse(state.dense.cells)

    def test_busy_dense_worker_never_blocks_frames_or_reset_and_drops_old_epoch(self):
        state = MapState(fusion_bypass=True)
        state.start_session(("test", 1), "test")
        entered, release = threading.Event(), threading.Event()
        original = state.dense.integrate
        def blocked(*args):
            with old.lock:
                entered.set()
                release.wait(2)
                original(*args)
        old = state.dense
        try:
            with patch.object(old, "integrate", side_effect=blocked):
                state.process(flat_frame())
                self.assertTrue(entered.wait(1))
                frame = flat_frame()
                for capture in range(1, 20):
                    frame.header = dict(frame.header, t_capture=capture, frame_id=capture)
                    started = time.monotonic()
                    state.process(frame)
                    self.assertLess(time.monotonic() - started, .05)
                self.assertEqual(state.latest_camera[2], 19)
                self.assertGreater(state.dense_worker.dropped, 0)
                started = time.monotonic()
                state.start_session(("new", 2), "new")
                self.assertLess(time.monotonic() - started, .05)
                self.assertIsNot(old, state.dense)
                release.set()
            state.dense_worker.close()
            self.assertFalse(state.dense.cells)
        finally:
            release.set()
            state.dense_worker.close()

    def test_kill_switch_and_rotating_measured_sample_budget(self):
        with patch.dict(os.environ, {"FARSIGHT_DENSE": "0"}):
            state = MapState(fusion_bypass=True)
        state.start_session(("test", 1), "test")
        state.process(flat_frame())
        self.assertFalse(state.dense_worker.enabled)
        self.assertIsNone(state.dense_worker.thread)
        self.assertFalse(state.dense.cells)
        self.assertEqual(state.latest_camera[2], 0)
        frame = flat_frame()
        xyz, rgb = measured_points(frame)
        a, colors = measured_points(frame, max_points=8, phase=0)
        b, _ = measured_points(frame, max_points=8, phase=1)
        self.assertEqual(len(a), 8)
        np.testing.assert_array_equal(a, xyz[::6])
        np.testing.assert_array_equal(colors, rgb[::6])
        self.assertFalse(np.array_equal(a, b))

    def test_worker_rate_limit_latest_only_and_point_budget(self):
        from jetson.mapping.demo import simulated_frame
        cloud = DenseMap(cache_only=True)
        worker = DenseWorker(lambda generation, candidate: candidate is cloud)
        frame = simulated_frame(1., 1)
        try:
            worker.offer(frame, cloud, 0)
            deadline = time.monotonic() + 1
            while not worker.processed and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertEqual(worker.processed, 1)
            self.assertLessEqual(len(cloud.cells), DENSE_FRAME_POINTS)
            for _ in range(20):
                worker.offer(frame, cloud, 0)
            self.assertEqual(worker.processed, 1)
            self.assertEqual(worker.status()["queued"], 1)
            self.assertGreaterEqual(worker.dropped, 19)
        finally:
            worker.close()

    def test_delta_pages_bootstrap_journal_gap_reset_and_concurrent_delete(self):
        cloud = DenseMap(journal_size=3)
        cloud.apply([(i * 20, 0, 0, 1, 2, 3, 1) for i in range(6)])
        meta, rows = unpack(cloud.delta(limit=2))
        self.assertTrue(meta["reset"])
        self.assertTrue(meta["snapshot"])
        self.assertEqual(len(rows), 2)
        consumer = DenseMap()
        consumer.apply(rows)
        original_anchor = meta["anchor"]
        cloud.apply([(0, 0, 0, 0, 0, 0, 0)])
        while meta["snapshot"]:
            meta, page = unpack(cloud.delta(meta["epoch"], meta["cursor"], True, 2, meta["anchor"]))
            rows += page
            self.assertFalse(meta["reset"])
            consumer.apply(page)
        self.assertEqual(meta["cursor"], original_anchor)
        next_meta, changes = unpack(cloud.delta(meta["epoch"], meta["cursor"]))
        self.assertEqual(changes[0][6], 0)
        consumer.apply(changes)
        self.assertEqual(len(consumer.cells), 5)
        self.assertFalse(next_meta["reset"])
        gap, _ = unpack(cloud.delta(meta["epoch"], 1))
        self.assertTrue(gap["reset"])

    def test_capacity_does_not_evict_mac_detail_and_ttl_sends_tombstones(self):
        now = [0.]
        cloud = DenseMap(capacity=2, cache_only=True, clock=lambda: now[0], ttl=30)
        epoch = cloud.epoch
        cloud.apply([(i * 20, 0, 0, 255, 0, 0, 1) for i in range(4)])
        self.assertEqual(len(cloud.cells), 2)
        _, rows = unpack(cloud.delta(epoch, 0))
        self.assertEqual(len(rows), 4)  # cache eviction is not a Mac deletion
        meta, _ = unpack(cloud.delta(epoch, 4))
        now[0] = 31
        _, rows = unpack(cloud.delta(epoch, meta["cursor"]))
        self.assertEqual([r[6] for r in rows], [0, 0])

    def test_fine_cell_updates_and_compressed_http_endpoint(self):
        app = create_app(fusion_bypass=True)
        cloud = app.state.mapping.dense
        cloud.apply([(i * 10, 0, 0, 20, 40, 60, 1) for i in range(100)])
        with TestClient(app) as client:
            response = client.get("/map/dense.bin?limit=11", headers={"Accept-Encoding": "br"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-encoding"], "br")
            meta, rows = unpack(response.content)
            self.assertEqual(len(rows), 11)
            header_size = int.from_bytes(response.content[:4], "little")
            self.assertEqual(len(response.content) - 4 - header_size, 11 * RECORD.size)
            response = client.get("/map/dense.bin?snapshot=true&epoch=" + cloud.epoch)
            self.assertEqual(response.status_code, 400)
            client.post("/map/reset")
            self.assertNotEqual(meta["epoch"], app.state.mapping.dense.epoch)


if __name__ == "__main__":
    unittest.main()
