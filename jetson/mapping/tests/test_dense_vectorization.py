"""Wire-order and lifetime compatibility with the previous measured admission."""

import unittest

import numpy as np

from jetson.mapping.dense import DenseMap, RECORD, unpack


class ReferenceDenseMap(DenseMap):
    def integrate(self, points, colors):
        if not len(points):
            return
        valid = np.isfinite(points).all(axis=1) & (np.abs(points) < 10000).all(axis=1)
        xyz = np.floor(points[valid] * 1000).astype(np.int32)
        rgb = np.asarray(colors[valid], np.uint8)
        _, keep = np.unique(np.floor_divide(xyz, 10), axis=0, return_index=True)
        self.apply([(*map(int, xyz[i]), *map(int, rgb[i]), 1) for i in keep])


class DenseVectorizationTests(unittest.TestCase):
    def pair(self, clock=lambda: 0., **kwargs):
        actual = DenseMap(clock=clock, **kwargs)
        reference = ReferenceDenseMap(clock=clock, **kwargs)
        reference.epoch = actual.epoch
        return actual, reference

    def same_state(self, actual, reference):
        self.assertEqual(list(actual.cells.items()), list(reference.cells.items()))
        self.assertEqual(list(actual.journal), list(reference.journal))
        self.assertEqual(actual.revision, reference.revision)

    def same_delta(self, actual, reference, **kwargs):
        actual_body = actual.delta(**kwargs)
        self.assertEqual(actual_body, reference.delta(**kwargs))
        return unpack(actual_body)

    def test_signed_numeric_order_first_sample_and_capacity_eviction_wire_bytes(self):
        # Input order and bytewise ordering disagree with signed numeric order;
        # matching raw pages also checks tombstone order after capacity eviction.
        mm = np.array([[2567, 4, -1], [-3, 8, -2], [-2574, 17, 7],
                       [-7, 1, -9], [2553, -19, 3], [-2571, 12, 9],
                       [-3, -4, 2563], [-3, -4, -2573], [0, 0, 0]], np.float64)
        points = mm / 1000
        colors = np.arange(len(points) * 3, dtype=np.uint8).reshape(-1, 3)
        for capacity in (2, len(points)):
            for cache_only in (False, True):
                with self.subTest(capacity=capacity, cache_only=cache_only):
                    actual, reference = self.pair(capacity=capacity, cache_only=cache_only)
                    actual.integrate(points, colors)
                    reference.integrate(points, colors)
                    self.same_state(actual, reference)
                    meta, rows = self.same_delta(actual, reference, epoch=actual.epoch)
                    upserts = [row for row in rows if row[6] == 1]
                    keys = [tuple(value // 10 for value in row[:3]) for row in upserts]
                    self.assertEqual(keys, sorted(keys))
                    # Both negative duplicates must retain the first observation.
                    first = next(row for row in upserts if tuple(v // 10 for v in row[:3]) == (-1, 0, -1))
                    self.assertEqual(first, (*map(int, np.floor(points[1] * 1000)), *map(int, colors[1]), 1))
                    self.assertEqual(meta['cursor'], actual.revision)
                    self.assertEqual(len(RECORD.pack(*first)), 16)

    def test_noncontiguous_rasters_and_random_duplicate_cells_match_full_packets(self):
        rng = np.random.default_rng(38)
        for dtype in (np.float32, np.float64):
            with self.subTest(dtype=dtype):
                actual, reference = self.pair(capacity=250, journal_size=10000)
                for _ in range(3):
                    # Millimetre noise makes different real samples share a cell.
                    cells = rng.integers(-300, 301, (800, 3))
                    cells[400:] = cells[:400]
                    points = (cells * .01 + rng.uniform(.001, .008, (800, 3))).astype(dtype)
                    storage = np.empty((800, 6), dtype=dtype)
                    storage[:, ::2] = points
                    colors = rng.integers(0, 256, (800, 6), dtype=np.uint8)[:, ::2]
                    actual.integrate(storage[:, ::2], colors)
                    reference.integrate(storage[:, ::2], colors)
                    self.same_state(actual, reference)
                    self.same_delta(actual, reference, epoch=actual.epoch)

    def test_invalid_samples_and_empty_admission_preserve_expiry_behavior(self):
        now = [0.]
        actual, reference = self.pair(clock=lambda: now[0], ttl=1)
        seed = np.array([[.001, .002, .003]])
        rgb = np.array([[10, 20, 30]], np.uint8)
        for cloud in (actual, reference):
            cloud.integrate(seed, rgb)
        now[0] = 2.
        empty = np.empty((0, 3))
        for cloud in (actual, reference):
            cloud.integrate(empty, np.empty((0, 3), np.uint8))
        self.same_state(actual, reference)
        self.assertEqual(len(actual.cells), 1)  # Empty input does not call apply.
        invalid = np.array([[np.nan, 0, 0], [0, np.inf, 0], [10000, 0, 0], [-10000, 0, 0]])
        for cloud in (actual, reference):
            cloud.integrate(invalid, np.zeros((4, 3), np.uint8))
        self.same_state(actual, reference)
        self.assertFalse(actual.cells)  # A nonempty invalid batch still expires.
        _, rows = self.same_delta(actual, reference, epoch=actual.epoch)
        self.assertEqual([row[6] for row in rows], [1, 0])

    def test_refresh_deletion_paginated_snapshot_and_clear_keep_identical_cursors(self):
        now = [0.]
        actual, reference = self.pair(clock=lambda: now[0], ttl=30, journal_size=5)
        points = np.array([[.04, 0, 0], [-.04, 0, 0], [.02, 0, 0], [-.02, 0, 0]])
        colors = np.tile(np.array([[11, 22, 33]], np.uint8), (4, 1))
        for t in (0., 4., 5.):
            now[0] = t
            for cloud in (actual, reference):
                cloud.integrate(points, colors)
            self.same_state(actual, reference)
        self.assertEqual(actual.revision, 8)  # Refresh coalescing and 5s renewal.
        meta, _ = self.same_delta(actual, reference, epoch=actual.epoch, after=0, limit=2)
        self.assertTrue(meta['reset'])  # The bounded journal requires a snapshot.
        self.assertTrue(meta['snapshot'])
        tombstone = (-40, 0, 0, 0, 0, 0, 0)
        for cloud in (actual, reference):
            cloud.apply([tombstone])
        while meta['snapshot']:
            meta, _ = self.same_delta(actual, reference, epoch=meta['epoch'],
                                     after=meta['cursor'], snapshot=True, limit=2, anchor=meta['anchor'])
        _, rows = self.same_delta(actual, reference, epoch=meta['epoch'], after=meta['cursor'])
        self.assertEqual(rows, [tombstone])
        now[0] = 36.
        self.same_delta(actual, reference, epoch=actual.epoch, after=actual.revision)
        self.same_state(actual, reference)
        self.assertFalse(actual.cells)
        for cloud in (actual, reference):
            cloud.clear(source={'session_id': 'new', 'map_epoch': 2})
        reference.epoch = actual.epoch
        for cloud in (actual, reference):
            cloud.integrate(points[::-1], colors[::-1])
        self.same_state(actual, reference)
        self.same_delta(actual, reference, limit=2)


if __name__ == '__main__':
    unittest.main()
