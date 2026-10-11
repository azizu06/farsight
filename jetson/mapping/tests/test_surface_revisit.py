import struct
from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from jetson.mapping.geometry import Calibration, POINT_DTYPE
from jetson.mapping.phone import RGBDFrame
from jetson.mapping.revisit import DepthView
from jetson.mapping.surfaces import SurfaceMap


def frame(depth=2):
    width, height = 32, 24
    values = np.full((height, width), depth, np.float32)
    return RGBDFrame({}, np.full((height, width, 3), 120, np.uint8), values,
                     np.full(values.shape, 2, np.uint8),
                     Calibration(width, height, 16, 16, 16, 12), np.eye(4), b"")


def plane(depth=2, color=30, x=0):
    points = np.array([[-.2 + x, -.2, -depth], [.2 + x, -.2, -depth],
                       [-.2 + x, .2, -depth], [.2 + x, .2, -depth]], np.float32)
    return points, np.full((4, 3), color, np.uint8), np.array([[0, 1, 2], [1, 3, 2]], np.uint32)


def coarse(mesh):
    return mesh[0][:3], mesh[1][:3], np.array([[0, 1, 2]], np.uint32)


def combine(*meshes):
    offset, indices = 0, []
    for points, _, faces in meshes:
        indices.append(faces + offset)
        offset += len(points)
    return (np.concatenate([m[0] for m in meshes]), np.concatenate([m[1] for m in meshes]),
            np.concatenate(indices))


def decode(packet):
    vertices, indices = struct.unpack_from("<II", packet)
    assert len(packet) == 8 + 16 * vertices + 4 * indices
    points = np.frombuffer(packet, POINT_DTYPE, vertices, 8)
    faces = np.frombuffer(packet, "<u4", indices, 8 + 16 * vertices).reshape(-1, 3)
    assert not indices or faces.max() < vertices
    return points, faces


def test_farther_background_replaces_old_foreground_at_same_pose():
    surfaces = SurfaceMap()
    old, new = plane(2, 10), plane(4, 200)
    surfaces.integrate(old, np.eye(4), {4: coarse(old)})
    plan = surfaces.prepare_revisit(DepthView.from_frame(frame(4)))
    # Preparation never edits history or the original capture arrays.
    assert surfaces.count == 2 and surfaces.revision == 1
    np.testing.assert_array_equal(old[0][:, 2], -2)
    surfaces.integrate(new, np.eye(4), {4: coarse(new)}, revisit=plan)
    assert surfaces.count == 2 and len(surfaces.keyframes) == len(surfaces.lods) == 1
    assert surfaces.revision == 2
    points, faces = decode(surfaces.encode())
    assert len(faces) == 2
    np.testing.assert_array_equal(points["xyz"][:, 2], -4)
    np.testing.assert_array_equal(points["rgb"], 200)
    coarse_points, coarse_faces = decode(surfaces.pack(surfaces.visual(1)))
    assert len(coarse_faces) == 1
    np.testing.assert_array_equal(coarse_points["xyz"][:, 2], -4)


def test_same_pose_refreshes_color_without_accumulating_keyframes():
    surfaces = SurfaceMap()
    surfaces.integrate(plane(2, 10), np.eye(4))
    # Omitting a measured revisit keeps the existing movement gate.
    surfaces.integrate(plane(2, 50), np.eye(4))
    assert surfaces.revision == 1
    for color in (80, 120, 240):
        plan = surfaces.prepare_revisit(DepthView.from_frame(frame(2)))
        surfaces.integrate(plane(2, color), np.eye(4), revisit=plan)
        assert surfaces.count == 2 and len(surfaces.keyframes) == 1
        points, _ = decode(surfaces.encode())
        np.testing.assert_array_equal(points["rgb"], color)


def test_occluded_out_of_view_and_unknown_faces_survive_refresh():
    surfaces = SurfaceMap()
    old = combine(plane(2, 10), plane(6, 60), plane(2, 90, x=10), plane(2, 120, x=-1.5))
    surfaces.integrate(old, np.eye(4), {4: old})
    current = frame(4)
    # The last plane projects near column 4, where this capture has no depth.
    current.confidence[:, :8] = 0
    plan = surfaces.prepare_revisit(DepthView.from_frame(current))
    surfaces.integrate(plane(4, 200), np.eye(4), {4: coarse(plane(4, 200))}, revisit=plan)
    assert surfaces.count == 8
    points, faces = decode(surfaces.encode())
    assert len(faces) == 8
    assert set(points["rgb"][:, 0]) == {60, 90, 120, 200}
    assert 10 not in points["rgb"]
    # Surviving captures and every stored LOD retain the same order.
    assert len(surfaces.keyframes) == len(surfaces.lods) == 2
    np.testing.assert_array_equal(surfaces.lods[0][4][1], surfaces.keyframes[0][1])


def test_face_with_one_contradicted_vertex_is_removed_and_vertices_compact():
    class PartialView:
        def observed(self, points):
            return np.zeros(len(points), bool)

        def contradicted(self, points):
            return points[:, 0] < 0

    mesh = plane()
    surfaces = SurfaceMap()
    surfaces.integrate(mesh, np.eye(4), {4: mesh})
    plan = surfaces.prepare_revisit(PartialView())
    empty = (np.empty((0, 3), np.float32), np.empty((0, 3), np.uint8), np.empty((0, 3), np.uint32))
    surfaces.integrate(empty, np.eye(4), revisit=plan)
    # Both faces touch a contradicted left vertex; no reconnection fills them.
    assert surfaces.count == 0 and not surfaces.keyframes and not surfaces.lods
    assert decode(surfaces.encode())[0].shape == (0,)
    assert surfaces.revision == 2


def test_partial_revisit_compacts_only_referenced_measured_vertices():
    class PartialView:
        def observed(self, points):
            return np.zeros(len(points), bool)

        def contradicted(self, points):
            return points[:, 2] > -3

    old = combine(plane(2, 10), plane(4, 20))
    surfaces = SurfaceMap()
    surfaces.integrate(old, np.eye(4), {8: old})
    plan = surfaces.prepare_revisit(PartialView())
    empty = plane()[0][:0], plane()[1][:0], plane()[2][:0]
    surfaces.integrate(empty, np.eye(4), revisit=plan)
    points, faces = decode(surfaces.encode())
    assert len(points) == 4 and len(faces) == surfaces.count == 2
    np.testing.assert_array_equal(points["xyz"], plane(4)[0])
    np.testing.assert_array_equal(faces, plane(4)[2])
    assert not surfaces.keyframes[0][0].flags.writeable
    assert len(surfaces.lods[0][8][0]) == 4


def test_triangle_budget_and_lod_alignment_after_replacement():
    surfaces = SurfaceMap(triangle_budget=4)
    old = plane(2)
    surfaces.integrate(old, np.eye(4), {4: coarse(old)})
    pose = np.eye(4)
    pose[0, 3] = .3
    unseen = plane(2, 90, x=10)
    surfaces.integrate(unseen, pose, {4: coarse(unseen)})
    plan = surfaces.prepare_revisit(DepthView.from_frame(frame(4)))
    new = plane(4, 200)
    surfaces.integrate(new, np.eye(4), {4: coarse(new)}, revisit=plan)
    assert surfaces.count == 4 and len(surfaces.keyframes) == len(surfaces.lods) == 2
    assert len(decode(surfaces.encode())[1]) == 4
    assert len(decode(surfaces.pack(surfaces.visual(2)))[1]) == 2
    # A new distant capture still retires the oldest whole aligned keyframe.
    pose[0, 3] = 1
    surfaces.integrate(plane(3, 150), pose, {4: coarse(plane(3, 150))})
    assert surfaces.count == 4 and len(surfaces.keyframes) == len(surfaces.lods) == 2
    assert all((m[1][:, 0] != 90).all() for m in surfaces.keyframes)


def test_repeated_revisits_bound_lod_only_history_without_changing_fine_export_count():
    surfaces = SurfaceMap(triangle_budget=4)
    pose = np.eye(4)
    # The coarse sample includes an unseen face, so it must survive even when
    # the capture's fine visible geometry is replaced by the next revisit.
    surfaces.integrate(plane(2, 10), pose, {4: coarse(plane(2, 10, x=10))})
    view = DepthView.from_frame(frame(4))
    for color in range(20, 40):
        plan = surfaces.prepare_revisit(view)
        surfaces.integrate(plane(4, color), pose,
                           {4: coarse(plane(4, color, x=10))}, revisit=plan)
        retained = sum(max(len(fine[2]), max(len(m[2]) for m in levels.values()))
                       for fine, levels in zip(surfaces.keyframes, surfaces.lods))
        assert retained <= surfaces.budget
        assert len(surfaces.keyframes) == len(surfaces.lods) <= 3
        assert len(decode(surfaces.encode())[1]) == surfaces.count == 2
    assert len(surfaces.keyframes) == 3
    assert [len(m[2]) for m in surfaces.keyframes] == [0, 0, 2]
    # The oldest out-of-view levels are retired as whole aligned captures;
    # surviving coarse exports retain only the three newest measured colors.
    points, faces = decode(surfaces.pack(surfaces.visual(3)))
    assert len(faces) == 3
    assert set(points["rgb"][:, 0]) == {37, 38, 39}
    assert surfaces._retained_weight == 4


def test_no_replacement_mesh_preserves_matching_surfaces_but_clears_contradictions():
    surfaces = SurfaceMap(triangle_budget=2)
    surfaces.integrate(plane(2), np.eye(4))
    empty = plane()[0][:0], plane()[1][:0], plane()[2][:0]
    surfaces.integrate(empty, np.eye(4), revisit=surfaces.prepare_revisit(DepthView.from_frame(frame(2))))
    assert surfaces.count == 2 and surfaces.revision == 1
    too_large = combine(plane(4), plane(4, x=.5))
    surfaces.integrate(too_large, np.eye(4), revisit=surfaces.prepare_revisit(DepthView.from_frame(frame(4))))
    assert surfaces.count == 0 and surfaces.revision == 2


def test_stale_plan_cannot_modify_newer_history_or_a_reset_map():
    surfaces = SurfaceMap()
    surfaces.integrate(plane(2), np.eye(4))
    plan = surfaces.prepare_revisit(DepthView.from_frame(frame(4)))
    with pytest.raises(FrozenInstanceError):
        plan.revision = 99
    pose = np.eye(4)
    pose[0, 3] = .3
    surfaces.integrate(plane(3, 80), pose)
    before = surfaces.encode(), surfaces.revision, surfaces.last_pose.copy()
    surfaces.integrate(plane(4), pose, revisit=plan)
    assert surfaces.encode() == before[0] and surfaces.revision == before[1]
    np.testing.assert_array_equal(surfaces.last_pose, before[2])
    # Revisions repeat after clear; the plan's epoch must still distinguish them.
    surfaces.clear()
    surfaces.integrate(plane(3, 80), np.eye(4))
    before = surfaces.encode()
    surfaces.integrate(plane(4), np.eye(4), revisit=plan)
    assert surfaces.encode() == before and surfaces.revision == 1
