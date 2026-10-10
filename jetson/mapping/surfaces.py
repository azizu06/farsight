"""Bounded measured surface keyframes, without filling unobserved holes."""

from collections import deque
import struct
import numpy as np

from .geometry import POINT_DTYPE


class SurfaceMap:
    def __init__(self, triangle_budget=160_000):
        self.budget = triangle_budget
        self.clear()

    def clear(self):
        self.keyframes = deque()
        self.lods = deque()  # {stride: coarser mesh of the same keyframe}, aligned with keyframes
        self.count = 0
        self.last_pose = None
        self.revision = 0

    def integrate(self, mesh, pose, lods=None):
        if not len(mesh[2]):
            return
        if self.last_pose is not None:
            delta = np.linalg.norm(pose[:3, 3] - self.last_pose[:3, 3])
            angle = np.arccos(np.clip((np.trace(pose[:3, :3].T @ self.last_pose[:3, :3]) - 1) / 2, -1, 1))
            if delta < 0.18 and angle < np.deg2rad(14):
                return
        if len(mesh[2]) > self.budget:
            return
        self.keyframes.append(mesh)
        self.lods.append(lods or {})
        self.count += len(mesh[2])
        while self.count > self.budget:
            self.count -= len(self.keyframes.popleft()[2])
            self.lods.popleft()
        self.last_pose = pose.copy()
        self.revision += 1

    def visual(self, limit):
        """Connected low-detail meshes for a `limit`-triangle visual export, or None.

        Uses the finest stored LOD at which every keyframe fits; past that, the
        newest whole keyframes at the coarsest LOD. Faces stay connected patches
        instead of scattered survivors of uniform triangle deletion.
        """
        steps = sorted(set.intersection(*(set(lod) for lod in self.lods))) if self.keyframes else []
        if not steps:
            return None
        for step in steps:
            meshes = [lod[step] for lod in self.lods]
            if sum(len(m[2]) for m in meshes) <= limit:
                return meshes
        kept, total = [], 0
        for mesh in reversed(meshes):
            if total + len(mesh[2]) > limit:
                break
            kept.append(mesh); total += len(mesh[2])
        return kept[::-1]

    @staticmethod
    def pack(meshes):
        vertices, indices, offset = [], [], 0
        for points, colors, faces in meshes:
            packed = np.zeros(len(points), dtype=POINT_DTYPE)
            packed["xyz"], packed["rgb"] = points, colors
            vertices.append(packed.tobytes())
            indices.append((faces.ravel() + offset).astype("<u4").tobytes())
            offset += len(points)
        return struct.pack("<II", offset, sum(len(m[2]) for m in meshes) * 3) + b"".join(vertices) + b"".join(indices)

    def encode(self):
        vertices, indices, offset = [], [], 0
        for points, colors, faces in self.keyframes:
            packed = np.zeros(len(points), dtype=POINT_DTYPE)
            packed["xyz"], packed["rgb"] = points, colors
            vertices.append(packed.tobytes())
            indices.append((faces.ravel() + offset).astype("<u4").tobytes())
            offset += len(points)
        return struct.pack("<II", offset, self.count * 3) + b"".join(vertices) + b"".join(indices)
