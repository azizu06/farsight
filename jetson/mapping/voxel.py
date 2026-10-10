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
        for i, key in enumerate(map(tuple, unique)):
            pos, color = xyz[i] / counts[i], rgb[i] / counts[i]
            if key in self.cells:
                old_pos, old_color, weight = self.cells[key]
                # Bound observation weight so new evidence can still update a cell.
                alpha = 1 / (min(weight, 30) + 1)
                pos = old_pos * (1 - alpha) + pos * alpha
                color = old_color * (1 - alpha) + color * alpha
                weight = min(weight + 1, 30)
            else:
                weight = 1
            self.cells[key] = (pos, color, weight)
            self.cells.move_to_end(key)
        while len(self.cells) > self.capacity:
            self.cells.popitem(last=False)

    def snapshot(self):
        if not self.cells:
            return np.empty((0, 3), np.float32), np.empty((0, 3), np.uint8)
        values = list(self.cells.values())
        return (np.array([v[0] for v in values], np.float32),
                np.clip([v[1] for v in values], 0, 255).astype(np.uint8))
