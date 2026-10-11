"""Bounded measured surface keyframes, without filling unobserved holes."""

from collections import deque
from dataclasses import dataclass
import struct
import numpy as np

from .geometry import POINT_DTYPE


@dataclass(frozen=True)
class SurfaceRevisit:
    """A prepared, version-bound refresh; projection finishes before applying it."""

    revision: int
    epoch: object
    original_keyframes: tuple
    original_lods: tuple
    refreshed: tuple
    contradicted: tuple
    refreshed_count: int
    contradicted_count: int
    refreshed_weights: tuple
    contradicted_weights: tuple


def _weight(mesh, levels):
    """Charge an aligned capture for its largest remaining detail level."""
    return max(len(mesh[2]), max((len(coarse[2]) for coarse in levels), default=0))


def _compact(mesh, keep):
    """Retain measured faces and their vertices without reconnecting any holes."""
    if keep.all():
        return mesh
    points, colors, faces = mesh
    faces = faces[keep]
    used, remapped = np.unique(faces.ravel(), return_inverse=True)
    compact = (points[used], colors[used], remapped.reshape(-1, 3).astype(mesh[2].dtype))
    for array in compact:
        array.setflags(write=False)
    return compact


def _refresh(mesh, view):
    """Prepare both replacement and removal-only versions of one mesh."""
    points, _, faces = mesh
    if not len(faces):
        return mesh, mesh
    observed = view.observed(points)
    contradicted = view.contradicted(points)
    retained = ~contradicted[faces].any(axis=1)
    replacement = retained & ~observed[faces].all(axis=1)
    removed = _compact(mesh, retained)
    refreshed = removed if np.array_equal(retained, replacement) else _compact(mesh, replacement)
    return refreshed, removed


class SurfaceMap:
    def __init__(self, triangle_budget=160_000):
        self.budget = triangle_budget
        self.clear()

    def clear(self):
        self.keyframes = deque()
        self.lods = deque()  # {stride: coarser mesh of the same keyframe}, aligned with keyframes
        self.count = 0
        self._weights = deque()
        self._retained_weight = 0
        self.last_pose = None
        self.revision = 0
        self._epoch = object()

    def prepare_revisit(self, view):
        """Project a surface snapshot outside the caller's map lock.

        Stored meshes are never modified in place. A concurrent integration or
        clear invalidates this plan, so a slow projection cannot edit newer
        history. Both fine and coarser keyframes are refreshed independently.
        """
        from .revisit import DepthView
        if not hasattr(view, "observed"):
            view = DepthView.from_frame(view)
        revision, epoch = self.revision, self._epoch
        keyframes, lods = tuple(self.keyframes), tuple(self.lods)
        refreshed, contradicted = [], []
        for mesh, levels in zip(keyframes, lods):
            updated, removed = _refresh(mesh, view)
            new_levels, removed_levels = [], []
            for stride, coarse in tuple(levels.items()):
                new_coarse, removed_coarse = _refresh(coarse, view)
                new_levels.append((stride, new_coarse))
                removed_levels.append((stride, removed_coarse))
            # A coarse mesh can retain an unseen face after all fine faces were
            # refreshed. Keep its alignment; discard only empty whole captures.
            if len(updated[2]) or any(len(coarse[2]) for _, coarse in new_levels):
                refreshed.append((updated, tuple(new_levels)))
            if len(removed[2]) or any(len(coarse[2]) for _, coarse in removed_levels):
                contradicted.append((removed, tuple(removed_levels)))
        return SurfaceRevisit(revision, epoch, keyframes, lods, tuple(refreshed),
                              tuple(contradicted), sum(len(m[2]) for m, _ in refreshed),
                              sum(len(m[2]) for m, _ in contradicted),
                              tuple(_weight(m, (coarse for _, coarse in levels)) for m, levels in refreshed),
                              tuple(_weight(m, (coarse for _, coarse in levels)) for m, levels in contradicted))

    def integrate(self, mesh, pose, lods=None, revisit=None):
        changed = False
        weight = _weight(mesh, (lods or {}).values())
        if revisit is not None:
            if (revisit.epoch is not self._epoch or revisit.revision != self.revision
                    or len(revisit.original_keyframes) != len(self.keyframes)
                    or len(revisit.original_lods) != len(self.lods)
                    or any(old is not current for old, current in zip(revisit.original_keyframes, self.keyframes))
                    or any(old is not current for old, current in zip(revisit.original_lods, self.lods))):
                return
            replacing = len(mesh[2]) > 0 and weight <= self.budget
            frames = revisit.refreshed if replacing else revisit.contradicted
            changed = (len(frames) != len(self.keyframes)
                       or any(updated is not old for (updated, _), old in zip(frames, self.keyframes))
                       or any(coarse is not old_levels.get(stride)
                              for (_, levels), old_levels in zip(frames, self.lods)
                              for stride, coarse in levels))
            if changed:
                self.keyframes = deque(m for m, _ in frames)
                self.lods = deque(dict(levels) for _, levels in frames)
                self.count = revisit.refreshed_count if replacing else revisit.contradicted_count
                self._weights = deque(revisit.refreshed_weights if replacing else revisit.contradicted_weights)
                self._retained_weight = sum(self._weights)
        if not len(mesh[2]):
            if changed:
                self.revision += 1
            return
        if revisit is None and self.last_pose is not None:
            delta = np.linalg.norm(pose[:3, 3] - self.last_pose[:3, 3])
            angle = np.arccos(np.clip((np.trace(pose[:3, :3].T @ self.last_pose[:3, :3]) - 1) / 2, -1, 1))
            if delta < 0.18 and angle < np.deg2rad(14):
                return
        if weight > self.budget:
            if changed:
                self.revision += 1
            return
        self.keyframes.append(mesh)
        self.lods.append(lods or {})
        self._weights.append(weight)
        self.count += len(mesh[2])
        self._retained_weight += weight
        while self._retained_weight > self.budget:
            self.count -= len(self.keyframes.popleft()[2])
            self.lods.popleft()
            self._retained_weight -= self._weights.popleft()
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
