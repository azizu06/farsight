import os
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

import numpy as np
from fastapi.testclient import TestClient

from jetson.mapping.geometry import POINT_DTYPE, encode_points
from jetson.mapping.server import create_app
from jetson.mapping.wire import cap_from_env, decimate_mesh_bin, decimate_points_bin


def points_bin(n):
    return encode_points(np.column_stack((np.arange(n), np.zeros(n), np.zeros(n))).astype(np.float32),
                         np.tile([1, 2, 3], (n, 1)).astype(np.uint8))


def decode_mesh(packet):
    vertices, indices = struct.unpack_from("<II", packet)
    records = np.frombuffer(packet, POINT_DTYPE, vertices, 8)
    faces = np.frombuffer(packet, "<u4", indices, 8 + 16 * vertices).reshape(-1, 3)
    return records, faces, len(packet) == 8 + 16 * vertices + 4 * indices


def grid_mesh(nx, ny, spacing, origin=(0.0, 0.0, 0.0)):
    xs, ys = np.meshgrid(np.arange(nx) * spacing, np.arange(ny) * spacing)
    points = (np.column_stack((xs.ravel(), ys.ravel(), np.zeros(nx * ny))) + origin).astype(np.float32)
    v = np.arange(nx * ny).reshape(ny, nx)
    a, b, c, d = v[:-1, :-1], v[:-1, 1:], v[1:, :-1], v[1:, 1:]
    faces = np.concatenate((np.stack((a, c, b), -1).reshape(-1, 3), np.stack((b, c, d), -1).reshape(-1, 3)))
    return points, np.full((len(points), 3), 200, np.uint8), faces.astype(np.uint32)


class DecimationParityTests(unittest.TestCase):
    """Same vectors as rust/farsight-server/src/geometry.rs cap_tests."""

    def test_points_stride_samples_and_preserves_records(self):
        raw = points_bin(100)
        capped = decimate_points_bin(raw, 10)
        self.assertEqual(struct.unpack_from("<I", capped)[0], 10)
        self.assertEqual(len(capped), 4 + 10 * 16)
        for i in range(10):  # i*100/10 -> 0, 10, 20 ...; x holds the original index
            self.assertEqual(struct.unpack_from("<f", capped, 4 + i * 16)[0], i * 10)
            self.assertEqual(capped[4 + i * 16 + 12:4 + i * 16 + 15], b"\x01\x02\x03")
        self.assertEqual(capped, decimate_points_bin(raw, 10))
        self.assertIs(decimate_points_bin(raw, 200), raw)
        self.assertIs(decimate_points_bin(raw, 0), raw)
        self.assertEqual(decimate_points_bin(struct.pack("<I", 0), 10), struct.pack("<I", 0))

    def test_points_uneven_stride_uses_floor(self):
        capped = decimate_points_bin(points_bin(7), 3)  # 0, 7//3=2, 14//3=4
        self.assertEqual([struct.unpack_from("<f", capped, 4 + i * 16)[0] for i in range(3)], [0, 2, 4])

    def test_mesh_keeps_wire_valid_and_remaps(self):
        raw = struct.pack("<II", 6, 6)
        for v in range(6):
            raw += struct.pack("<fff", v, 0, 0) + bytes((9, 9, 9, 0))
        raw += struct.pack("<6I", 0, 1, 2, 3, 4, 5)
        capped = decimate_mesh_bin(raw, 1)
        self.assertEqual(struct.unpack_from("<II", capped), (3, 3))
        self.assertEqual(len(capped), 8 + 3 * 16 + 3 * 4)
        self.assertEqual(struct.unpack_from("<3I", capped, 8 + 3 * 16), (0, 1, 2))
        self.assertEqual(capped, decimate_mesh_bin(raw, 1))
        self.assertIs(decimate_mesh_bin(raw, 5), raw)
        self.assertIs(decimate_mesh_bin(raw, 0), raw)

    def test_mesh_compacts_shared_vertices_in_first_use_order(self):
        # triangles (4,1,0) and (1,4,3) share vertices 1 and 4; stride picks both of 4 triangles
        raw = struct.pack("<II", 5, 12)
        for v in range(5):
            raw += struct.pack("<fff", v, v, v) + bytes((v, v, v, 0))
        raw += struct.pack("<12I", 4, 1, 0, 2, 2, 2, 1, 4, 3, 0, 0, 0)
        records, faces, exact = decode_mesh(decimate_mesh_bin(raw, 2))
        self.assertTrue(exact)
        self.assertEqual(records["xyz"][:, 0].tolist(), [4, 1, 0, 3])
        self.assertEqual(faces.tolist(), [[0, 1, 2], [1, 0, 3]])


class RouteTests(unittest.TestCase):
    def fill(self, app, n_points=50_000):
        state = app.state.mapping
        state.cloud.capacity = 200_000
        rng = np.random.default_rng(4)
        state.cloud.integrate(rng.uniform((-10, 0, -10), (10, 3, 10), (n_points, 3)),
                              rng.integers(0, 255, (n_points, 3)))
        for i in range(3):
            pose = np.eye(4)
            pose[0, 3] = i * 2.0
            state.surfaces.integrate(grid_mesh(120, 100, .01, (i * 1.5, 0, 0)), pose)
        state.revision += 1
        return state

    def test_health_remains_responsive_while_points_are_encoded(self):
        app = create_app(detector="none", map_points_cap=0)
        state = app.state.mapping
        state.cloud.integrate(np.array([[.25, .5, .75]]), np.array([[10, 20, 30]], np.uint8))
        state.revision += 1
        encoding, release = threading.Event(), threading.Event()

        def blocked_encode(*args):
            encoding.set()
            release.wait(3)
            return encode_points(*args)

        with TestClient(app) as client, ThreadPoolExecutor(2) as pool:
            with patch("jetson.mapping.state.encode_points", side_effect=blocked_encode):
                points = pool.submit(client.get, "/map/points.bin")
                try:
                    self.assertTrue(encoding.wait(2), "Point encoder did not start")
                    health = pool.submit(client.get, "/health")
                    self.assertEqual(health.result(timeout=.5).json()["points"], 1)
                    reset = pool.submit(client.post, "/map/reset")
                    self.assertEqual(reset.result(timeout=.5).json()["points"], 0)
                finally:
                    release.set()
                self.assertEqual(points.result().content, encode_points(
                    np.array([[.25, .5, .75]], np.float32), np.array([[10, 20, 30]], np.uint8)))
                self.assertEqual(client.get("/map/points.bin").content, struct.pack("<I", 0))

    def test_capped_routes_status_and_full_internal_map(self):
        app = create_app(detector="none", map_points_cap=5000, map_triangles_cap=20000)
        state = self.fill(app)
        cells, triangles = len(state.cloud.cells), state.surfaces.count
        self.assertGreater(cells, 5000)
        self.assertGreater(triangles, 20000)
        with TestClient(app) as client:
            points = client.get("/map/points.bin", headers={"Accept-Encoding": "br"})
            self.assertEqual(points.headers["content-encoding"], "br")  # httpx decodes the body
            self.assertEqual(struct.unpack_from("<I", points.content)[0], 5000)
            self.assertEqual(len(points.content), 4 + 5000 * 16)
            records, faces, exact = decode_mesh(client.get("/map/mesh.bin").content)
            self.assertTrue(exact)
            self.assertEqual(len(faces), 20000)
            self.assertLess(faces.max(), len(records))
            ply = client.get("/map/export.ply").content
            self.assertIn(b"element vertex 5000\n", ply)
            self.assertEqual(ply[ply.index(b"end_header\n") + 11:][:12], points.content[4:16])
            health = client.get("/health").json()
            self.assertTrue(health["capped"])
            self.assertEqual((health["points"], health["triangles"]), (cells, triangles))
            with client.websocket_connect("/ws/map?points=http") as ws:
                self.assertTrue(ws.receive_json()["capped"])
        self.assertEqual((len(state.cloud.cells), state.surfaces.count), (cells, triangles))

    def test_env_defaults_and_zero_disables(self):
        with patch.dict(os.environ, {"FARSIGHT_MAP_POINTS_CAP": "", "FARSIGHT_MAP_TRIANGLES_CAP": ""}):
            app = create_app(detector="none")
        self.assertEqual((app.state.mapping.points_cap, app.state.mapping.triangles_cap), (30_000, 40_000))
        with patch.dict(os.environ, {"FARSIGHT_MAP_POINTS_CAP": "0", "FARSIGHT_MAP_TRIANGLES_CAP": "0"}):
            app = create_app(detector="none")
        state = self.fill(app)
        with TestClient(app) as client:
            points = client.get("/map/points.bin").content
            self.assertEqual(struct.unpack_from("<I", points)[0], len(state.cloud.cells))
            self.assertEqual(len(decode_mesh(client.get("/map/mesh.bin").content)[1]), state.surfaces.count)
            self.assertFalse(client.get("/health").json()["capped"])
        with patch.dict(os.environ, {"FARSIGHT_MAP_POINTS_CAP": "123"}):
            self.assertEqual(create_app(detector="none", map_points_cap=7).state.mapping.points_cap, 7)

    def test_small_maps_are_uncapped(self):
        with TestClient(create_app(detector="none")) as client:
            self.assertEqual(client.get("/map/points.bin").content, struct.pack("<I", 0))
            self.assertFalse(client.get("/health").json()["capped"])

    def test_invalid_caps_fail_loudly(self):
        for bad in ("-1", "1.5", "lots"):
            with patch.dict(os.environ, {"FARSIGHT_MAP_POINTS_CAP": bad}):
                with self.assertRaises(ValueError):
                    cap_from_env("FARSIGHT_MAP_POINTS_CAP", 30_000)
        with self.assertRaises(ValueError):
            create_app(detector="none", map_triangles_cap=-1)


if __name__ == "__main__":
    unittest.main()
