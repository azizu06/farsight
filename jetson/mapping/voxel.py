"""Bounded spatial accumulation; points are averaged only within the same voxel."""

from collections import OrderedDict
import numpy as np


class VoxelMap:
    def __init__(self, size=0.04, capacity=120_000):
        if not np.isfinite(size) or size <= 0 or capacity <= 0:
            raise ValueError("Voxel size and capacity must be positive")
        self.size, self.capacity = size, capacity
        self.cells = OrderedDict()

    def clear(self):
        self.cells.clear()

    def integrate(self, points, colors):
        if not len(points):
            return
        finite = np.isfinite(points).all(axis=1)
        points, colors = points[finite], colors[finite]
        keys = np.floor(points / self.size).astype(np.int64)
        unique, inverse, counts = np.unique(keys, axis=0, return_inverse=True, return_counts=True)
        xyz, rgb = np.zeros((len(unique), 3)), np.zeros((len(unique), 3))
        np.add.at(xyz, inverse, points)
        np.add.at(rgb, inverse, colors)
        # Do arithmetic once per capture, rather than thousands of tiny NumPy
        # operations under the GIL that delay camera and control workers.
        xyz /= counts[:, None]
        rgb /= counts[:, None]
        keys = list(map(tuple, unique))
        previous = [self.cells.get(key) for key in keys]
        present = np.array([i for i, value in enumerate(previous) if value is not None], dtype=np.intp)
        weights = np.ones(len(keys), dtype=np.int64)
        if len(present):
            old_xyz = np.array([previous[i][0] for i in present])
            old_rgb = np.array([previous[i][1] for i in present])
            old_weights = np.array([previous[i][2] for i in present])
            # Bound observation weight so new evidence can still update a cell.
            alpha = (1 / (np.minimum(old_weights, 30) + 1))[:, None]
            xyz[present] = old_xyz * (1 - alpha) + xyz[present] * alpha
            rgb[present] = old_rgb * (1 - alpha) + rgb[present] * alpha
            weights[present] = np.minimum(old_weights + 1, 30)
        for key, position, color, weight in zip(keys, xyz, rgb, weights):
            # A retained voxel owns its small arrays, never an entire capture batch.
            self.cells[key] = (position.copy(), color.copy(), int(weight))
            self.cells.move_to_end(key)
        while len(self.cells) > self.capacity:
            self.cells.popitem(last=False)

    def snapshot(self):
        if not self.cells:
            return np.empty((0, 3), np.float32), np.empty((0, 3), np.uint8)
        values = list(self.cells.values())
        return (np.array([v[0] for v in values], np.float32),
                np.clip([v[1] for v in values], 0, 255).astype(np.uint8))
