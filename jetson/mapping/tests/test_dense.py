import unittest

import numpy as np
from fastapi.testclient import TestClient

from jetson.mapping.dense import DenseMap, RECORD, unpack
from jetson.mapping.phone import measured_points
from jetson.mapping.server import create_app
from jetson.mapping.state import MapState
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
        state.start_session(("test", 1), "test")
        state.process(frame)
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
            self.assertNotEqual(meta["epoch"], cloud.epoch)


if __name__ == "__main__":
    unittest.main()
