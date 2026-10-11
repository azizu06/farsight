"""Bounded spatial accumulation; points are averaged only within the same voxel."""

from collections import OrderedDict
import numpy as np


class VoxelMap:
    def __init__(self, size=0.04, capacity=120_000):
        if not np.isfinite(size) or size <= 0 or capacity <= 0:
            raise ValueError("Voxel size and capacity must be positive")
        self.size, self.capacity = size, capacity
        self.cells = OrderedDict()
        # Mirror wire-ready values in bounded buffers. Snapshot never walks the
        # Python cells; order tracks the same least-recently-updated iteration.
        self._rows = {}
        self._xyz = np.empty((capacity, 3), np.float32)
        self._rgb = np.empty((capacity, 3), np.uint8)
        self._order = np.empty(0, np.intp)
        self._touched = np.zeros(capacity, bool)
        self._sequence = np.empty(capacity, np.intp)

    def clear(self):
        self.cells.clear()
        self._rows.clear()
        self._order = np.empty(0, np.intp)

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
        # Callers may raise the visual capacity before integration.
        if self.capacity > len(self._xyz):
            extra = self.capacity - len(self._xyz)
            self._xyz = np.concatenate((self._xyz, np.empty((extra, 3), np.float32)))
            self._rgb = np.concatenate((self._rgb, np.empty((extra, 3), np.uint8)))
            self._touched = np.zeros(self.capacity, bool)
            self._sequence = np.empty(self.capacity, np.intp)
        touched = []
        clipped = np.clip(rgb, 0, 255).astype(np.uint8)
        for i, (key, position, color, weight) in enumerate(zip(keys, xyz, rgb, weights)):
            row = self._rows.get(key)
            if row is None:
                if len(self._rows) == self.capacity:
                    oldest, _ = self.cells.popitem(last=False)
                    row = self._rows.pop(oldest)
                else:
                    row = len(self._rows)
                self._rows[key] = row
            self._xyz[row], self._rgb[row] = position, clipped[i]
            self._sequence[row] = i
            touched.append(row)
            # A retained voxel owns its small arrays, never an entire capture batch.
            self.cells[key] = (position.copy(), color.copy(), int(weight))
            self.cells.move_to_end(key)
        # A capture larger than capacity can recycle a row more than once.
        # Keep only its last write, in exactly the OrderedDict's final order.
        updated = np.unique(np.asarray(touched, dtype=np.intp))
        updated = updated[np.argsort(self._sequence[updated])]
        self._touched[updated] = True
        self._order = np.concatenate((self._order[~self._touched[self._order]], updated))
        self._touched[updated] = False

    def snapshot(self):
        return self._xyz[self._order], self._rgb[self._order]
