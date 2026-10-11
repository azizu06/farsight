"""Capture weighting, retention and array ownership of the visual voxel cloud."""

import unittest

import numpy as np

from jetson.mapping.voxel import VoxelMap


class VoxelIntegrationTests(unittest.TestCase):
    def test_colliding_pixels_count_as_one_capture_before_next_capture_average(self):
        cloud = VoxelMap(size=1)
        cloud.integrate(np.array([[.125, .25, .5], [.375, .25, .5], [.25, .25, .5]]),
                        np.array([[0, 10, 20], [100, 110, 120], [200, 210, 220]], np.uint8))
        position, color, weight = cloud.cells[(0, 0, 0)]
        np.testing.assert_array_equal(position, [.25, .25, .5])
        np.testing.assert_array_equal(color, [100, 110, 120])
        self.assertEqual(weight, 1)
        cloud.integrate(np.array([[.75, .5, .25]]), np.array([[0, 10, 20]], np.uint8))
        position, color, weight = cloud.cells[(0, 0, 0)]
        np.testing.assert_array_equal(position, [.5, .375, .375])
        np.testing.assert_array_equal(color, [50, 60, 70])
        self.assertEqual(weight, 2)

    def test_updated_cells_keep_sorted_capture_order_and_evict_least_recent(self):
        cloud = VoxelMap(size=1, capacity=2)
        def capture(xs):
            points = np.array([[x, 0, 0] for x in xs])
            cloud.integrate(points, np.zeros((len(xs), 3), np.uint8))
        capture([1.25, .25])
        self.assertEqual(list(cloud.cells), [(0, 0, 0), (1, 0, 0)])
        capture([.25])
        self.assertEqual(list(cloud.cells), [(1, 0, 0), (0, 0, 0)])
        capture([2.25])
        self.assertEqual(list(cloud.cells), [(0, 0, 0), (2, 0, 0)])
        capture([1.25, .25])
        self.assertEqual(list(cloud.cells), [(0, 0, 0), (1, 0, 0)])

    def test_weight_cap_keeps_admitting_new_evidence(self):
        cloud = VoxelMap(size=1)
        for _ in range(40):
            cloud.integrate(np.array([[.25, .25, .25]]), np.array([[31, 62, 93]], np.uint8))
        self.assertEqual(cloud.cells[(0, 0, 0)][2], 30)
        cloud.integrate(np.array([[.75, .75, .75]]), np.array([[0, 0, 0]], np.uint8))
        position, color, weight = cloud.cells[(0, 0, 0)]
        np.testing.assert_array_equal(position, np.full(3, .25 * (1 - 1 / 31) + .75 / 31))
        np.testing.assert_allclose(color, np.array([31, 62, 93]) * (1 - 1 / 31), rtol=0, atol=1e-12)
        self.assertEqual(weight, 30)

    def test_zero_weight_is_replaced_and_clear_removes_all_history(self):
        cloud = VoxelMap(size=1)
        cloud.cells[(0, 0, 0)] = (np.full(3, .75), np.full(3, 200.), 0)
        cloud.integrate(np.array([[.25, .25, .25]]), np.array([[10, 20, 30]], np.uint8))
        position, color, weight = cloud.cells[(0, 0, 0)]
        np.testing.assert_array_equal(position, [.25, .25, .25])
        np.testing.assert_array_equal(color, [10, 20, 30])
        self.assertEqual(weight, 1)
        cloud.clear()
        points, colors = cloud.snapshot()
        self.assertEqual(points.shape, (0, 3))
        self.assertEqual(colors.shape, (0, 3))
        self.assertEqual((points.dtype, colors.dtype), (np.dtype('float32'), np.dtype('uint8')))

    def test_cells_own_small_arrays_independent_of_input_batch_and_snapshot(self):
        cloud = VoxelMap(size=1)
        points = np.array([[.25, .25, .25], [1.25, .25, .25]])
        colors = np.array([[10, 20, 30], [40, 50, 60]], np.uint8)
        cloud.integrate(points, colors)
        cells = list(cloud.cells.values())
        for component in (0, 1):
            self.assertFalse(np.shares_memory(cells[0][component], cells[1][component]))
            self.assertIsNone(cells[0][component].base, 'A surviving voxel retained an entire input batch')
        snapshot = cloud.snapshot()
        points[:] = 100; colors[:] = 0
        np.testing.assert_array_equal(cloud.snapshot()[0], snapshot[0])
        np.testing.assert_array_equal(cloud.snapshot()[1], snapshot[1])
        cloud.integrate(np.array([[.75, .75, .75]]), np.array([[100, 100, 100]], np.uint8))
        np.testing.assert_array_equal(cells[0][0], [.25, .25, .25])
        snapshot[0][:] = 100; snapshot[1][:] = 0
        self.assertLess(cloud.snapshot()[0].max(), 2)
        self.assertGreater(cloud.snapshot()[1].max(), 0)

    def test_snapshot_matches_ordered_wire_values_after_recycling_and_reset(self):
        cloud = VoxelMap(size=1, capacity=3)
        for xs in ([.25, 1.25, 2.25], [.75], [1.75, 3.25], list(np.arange(9) + .25)):
            points = np.column_stack((xs, np.zeros(len(xs)), np.zeros(len(xs))))
            colors = np.column_stack((np.arange(len(xs)) * 70.5 - 1, xs, xs))
            cloud.integrate(points, colors)
            # Reference the original ordered-cell wire conversion, including
            # float32 rounding, RGB clipping and truncation of averaged colors.
            values = list(cloud.cells.values())
            expected = (np.array([v[0] for v in values], np.float32),
                        np.clip([v[1] for v in values], 0, 255).astype(np.uint8))
            for actual, reference in zip(cloud.snapshot(), expected):
                self.assertEqual(actual.tobytes(), reference.tobytes())
        old = cloud.snapshot()
        cloud.clear()
        cloud.integrate(np.array([[20.25, 0, 0]]), np.array([[11, 22, 33]], np.uint8))
        np.testing.assert_array_equal(cloud.snapshot()[0], [[20.25, 0, 0]])
        np.testing.assert_array_equal(cloud.snapshot()[1], [[11, 22, 33]])
        self.assertEqual(len(old[0]), 3)

    def test_empty_and_nonfinite_samples_do_not_change_existing_cells(self):
        cloud = VoxelMap(size=1)
        cloud.integrate(np.array([[.25, .25, .25]]), np.full((1, 3), 100, np.uint8))
        expected = cloud.snapshot()
        cloud.integrate(np.empty((0, 3)), np.empty((0, 3), np.uint8))
        cloud.integrate(np.array([[np.nan, 1, 2], [1, np.inf, 2]]), np.zeros((2, 3), np.uint8))
        np.testing.assert_array_equal(cloud.snapshot()[0], expected[0])
        np.testing.assert_array_equal(cloud.snapshot()[1], expected[1])


if __name__ == '__main__':
    unittest.main()
