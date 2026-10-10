"""Real loopback relay joins: session safety, binary compatibility and control isolation."""
import asyncio
import json
from pathlib import Path
import struct
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tools.geometry_relay import geometry_origin, identity, validate_mesh
from tools.operator_relay import create_app

SESSION = "12345678-1234-4321-9876-123456789abc"
OTHER = "22345678-1234-4321-9876-123456789abc"


def mesh():
    records = b"".join(struct.pack("<3f4B", x, y, z, 120, 140, 160, 0)
                       for x, y, z in ((0, 0, 0), (1, 0, 0), (0, 1, 0)))
    return struct.pack("<II", 3, 3) + records + struct.pack("<3I", 0, 1, 2)


class GeometryRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        (Path(self.temp.name) / "index.html").write_text("Quest fixture")
        self.jetson = dict(type="status", source="IPHONE LIDAR", device="Real phone fixture", session_id=SESSION.upper(),
                           map_epoch=1, generation=7, revision=42, mesh_revision=9, points=0, triangles=0,
                           connected=True, fresh=True, tracking="normal", paused=False, frames=50,
                           rejected=0, dropped=0, processing_ms=2, age_ms=10, position=[1, 2, 3],
                           transform=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 1, 2, 3, 1],
                           depth_range=[.5, 2], reason=None)
        self.geo = dict(revision=1, session=SESSION.replace("-", ""), epoch=1, anchors=1, triangles=1,
                        last_seq=3, lane=dict(upstream="ws://phone:8083/geometry/stream"), snapshot_complete=True)
        self.body = mesh()
        self.mesh_reads = 0
        self.status_failure = False
        self.block = asyncio.Event()
        self.block.set()
        self.geo_request = asyncio.Event()
        self.rotate_during_read = False
        self.mutations = 0
        async def status(_):
            return web.json_response(self.jetson)
        async def geo_status(_):
            self.geo_request.set()
            await self.block.wait()
            if self.status_failure:
                raise web.HTTPServiceUnavailable()
            return web.json_response(self.geo)
        async def geo_mesh(_):
            self.mesh_reads += 1
            if self.rotate_during_read:
                self.geo["session"] = OTHER
                self.geo["revision"] += 1
            return web.Response(body=self.body, content_type="application/octet-stream")
        async def control(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"session_id": "control-authority", "permit": "real-upstream-permit"})
            async for message in ws:
                await ws.send_str(message.data)
            return ws
        async def camera(_):
            return web.Response(body=b"upstream camera", headers={"X-Frame-Source": "IPHONE LIDAR"})
        async def empty_jetson_mesh(_):
            return web.Response(body=b"WRONG EMPTY JETSON MAP")
        async def mutation(_):
            self.mutations += 1
            return web.json_response({"cleared": True})
        upstream = web.Application()
        upstream.router.add_get("/map/status", status)
        upstream.router.add_get("/map/mesh.bin", empty_jetson_mesh)
        upstream.router.add_get("/camera.jpg", camera)
        upstream.router.add_get("/ws/control", control)
        upstream.router.add_post("/map/reset", mutation)
        upstream.router.add_post("/map/pause", mutation)
        geometry = web.Application()
        geometry.router.add_get("/geometry/status", geo_status)
        geometry.router.add_get("/geometry/mesh.bin", geo_mesh)
        self.upstream = TestServer(upstream)
        self.geometry = TestServer(geometry)
        await self.upstream.start_server()
        await self.geometry.start_server()
        self.app = create_app(str(self.upstream.make_url("/")), dist=self.temp.name,
                              geometry_upstream=str(self.geometry.make_url("/")))
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.adapter = self.app["geometry"]
        self.adapter.interval = .02

    async def asyncTearDown(self):
        self.block.set()
        await self.client.close()
        await self.upstream.close()
        await self.geometry.close()
        self.temp.cleanup()

    async def wait(self, check):
        async with asyncio.timeout(3):
            while not check():
                await asyncio.sleep(.01)

    async def test_thin_status_and_exact_mesh_points_export(self):
        await self.wait(lambda: self.adapter.mesh is not None)
        async with self.client.ws_connect("/ws/map?points=http") as ws:
            packet = await ws.receive_json()
            self.assertEqual((packet["triangles"], packet["points"]), (1, 3))
            self.assertEqual(packet["session_id"], SESSION.upper())
            self.assertEqual(packet["transform"], self.jetson["transform"])
            self.assertEqual(packet["tracking"], "normal")
            self.assertEqual(packet["source"], "IPHONE LIDAR")
            self.assertEqual(packet["geometry_source"], "IPHONE ARKIT MESH")
            self.assertTrue(packet["geometry"]["matched"])
            self.assertEqual(packet["points_transport"], "http")
        response = await self.client.get("/map/mesh.bin")
        self.assertEqual(await response.read(), mesh())
        self.assertEqual(response.headers["X-Geometry-Source"], "IPHONE ARKIT MESH")
        self.assertEqual(response.headers["X-Map-Session"], SESSION)
        self.assertEqual(response.headers["X-Map-Epoch"], "1")
        points = await self.client.get("/map/points.bin")
        self.assertEqual(await points.read(), struct.pack("<I", 3) + mesh()[8:56])
        export = await self.client.get("/map/export.ply")
        ply = await export.read()
        self.assertIn(b"element vertex 3\n", ply)
        self.assertIn(b"element face 1\n", ply)
        self.assertTrue(ply.endswith(b"\x03" + struct.pack("<3I", 0, 1, 2)))
        camera = await self.client.get("/camera.jpg")
        self.assertEqual(await camera.read(), b"upstream camera")
        count = self.mesh_reads
        await asyncio.sleep(.08)
        self.assertEqual(self.mesh_reads, count, "stable revision redownloaded mesh")

    async def test_wrong_session_or_epoch_clears_without_fetching_geometry(self):
        await self.wait(lambda: self.adapter.mesh is not None)
        old_generation = self.adapter.generation
        reads = self.mesh_reads
        self.jetson["session_id"] = OTHER
        await self.wait(lambda: self.adapter.generation > old_generation)
        self.assertIsNone(self.adapter.mesh)
        response = await self.client.get("/map/mesh.bin")
        self.assertEqual(response.status, 409)
        self.assertEqual((await self.client.get("/map/export.ply")).status, 409)
        self.assertEqual(self.mesh_reads, reads)
        self.assertEqual(self.adapter.status()["triangles"], 0)
        self.geo.update(session=OTHER, epoch=2)
        await asyncio.sleep(.08)
        self.assertIsNone(self.adapter.mesh)
        self.jetson["map_epoch"] = 2
        await self.wait(lambda: self.adapter.mesh is not None)
        self.assertEqual(self.adapter.status()["map_epoch"], 2)

    async def test_pending_snapshot_and_unknown_completeness_are_explicit(self):
        await self.wait(lambda: self.adapter.mesh is not None)
        self.geo["snapshot_complete"] = False
        generation = self.adapter.generation
        await self.wait(lambda: self.adapter.mesh is None)
        self.assertGreater(self.adapter.generation, generation)
        self.assertEqual(self.adapter.status()["geometry"]["snapshot_complete"], False)
        del self.geo["snapshot_complete"]
        self.geo["revision"] += 1
        await self.wait(lambda: self.adapter.mesh is not None)
        self.assertIsNone(self.adapter.status()["geometry"]["snapshot_complete"])
        self.assertIn("not reported", self.adapter.status()["geometry"]["reason"])

    async def test_retained_matching_geometry_does_not_refresh_pose(self):
        await self.wait(lambda: self.adapter.mesh is not None)
        self.status_failure = True
        self.jetson.update(fresh=False, connected=False, tracking="stale")
        await self.wait(lambda: self.adapter.geometry_error and not self.adapter.status()["fresh"])
        self.assertTrue(self.adapter.status()["geometry"]["retained"])
        response = await self.client.get("/map/mesh.bin")
        self.assertEqual(await response.read(), mesh())
        self.assertEqual(response.headers["X-Geometry-Retained"], "true")

    async def test_mesh_identity_change_during_download_never_publishes(self):
        await self.wait(lambda: self.adapter.mesh is not None)
        self.rotate_during_read = True
        self.geo["revision"] += 1
        await self.wait(lambda: self.adapter.mesh is None)
        self.assertEqual(self.adapter.status()["triangles"], 0)

    async def test_slow_geometry_does_not_block_control_or_camera(self):
        await self.wait(lambda: self.adapter.mesh is not None)
        self.geo_request.clear()
        self.block.clear()
        await self.wait(lambda: self.geo_request.is_set())
        async with asyncio.timeout(.5):
            async with self.client.ws_connect("/ws/control") as ws:
                self.assertEqual((await ws.receive_json())["permit"], "real-upstream-permit")
                await ws.send_json({"deadman": False, "seq": 7})
                self.assertEqual((await ws.receive_json())["seq"], 7)
            camera = await self.client.get("/camera.jpg")
            self.assertEqual(await camera.read(), b"upstream camera")

    async def test_malformed_mesh_cannot_replace_valid_cache(self):
        await self.wait(lambda: self.adapter.mesh is not None)
        self.body = mesh()[:-1]
        self.geo["revision"] += 1
        await self.wait(lambda: self.adapter.geometry_error is not None)
        self.assertEqual(self.adapter.mesh, mesh())
        self.assertTrue(self.adapter.status()["geometry"]["retained"])
        self.assertEqual(self.adapter.status()["geometry"]["revision"], 1)
        self.assertEqual(self.adapter.status()["geometry"]["receiver_revision"], 2)

    async def test_lane_disconnect_and_authoritative_empty_mesh(self):
        await self.wait(lambda: self.adapter.mesh is not None)
        self.geo["lane"]["upstream"] = None
        await self.wait(lambda: self.adapter.status()["geometry"]["retained"])
        self.assertFalse(self.adapter.status()["geometry"]["connected"])
        self.assertIn("retained", self.adapter.status()["geometry"]["reason"])
        generation = self.adapter.generation
        self.geo.update(revision=2, anchors=0, triangles=0)
        self.body = struct.pack("<II", 0, 0)
        await self.wait(lambda: self.adapter.mesh == self.body)
        self.assertGreater(self.adapter.generation, generation)
        self.assertEqual(self.adapter.status()["points"], 0)

    async def test_reset_and_pause_do_not_pretend_to_clear_phone_reconstruction(self):
        await self.wait(lambda: self.adapter.mesh is not None)
        for path in ("/map/reset", "/map/pause"):
            response = await self.client.post(path, json={"paused": True})
            self.assertEqual(response.status, 409)
            self.assertIn("phone capture", await response.text())
        self.assertEqual(self.mutations, 0)
        self.assertEqual(self.adapter.mesh, mesh())
        legacy = TestClient(TestServer(create_app(str(self.upstream.make_url('/')), dist=self.temp.name)))
        await legacy.start_server()
        try:
            self.assertEqual((await legacy.post('/map/reset')).status, 200)
            self.assertEqual((await legacy.post('/map/pause', json={'paused': True})).status, 200)
            self.assertEqual(self.mutations, 2)
        finally:
            await legacy.close()


class GeometryValidationTests(unittest.TestCase):
    def test_loopback_origin_and_uuid_normalization(self):
        for url in ("http://127.0.0.1:8484", "http://localhost:8484", "http://[::1]:8484"):
            self.assertEqual(str(geometry_origin(url)), url)
        for url in ("http://example.com", "http://127.0.0.1.evil", "http://192.168.1.2", "http://user:pass@127.0.0.1", "http://localhost/path", "http://localhost?x=1"):
            with self.assertRaises(ValueError):
                geometry_origin(url)
        self.assertEqual(identity(SESSION.upper(), 1), identity(SESSION.replace("-", ""), 1))
        self.assertIsNone(identity(SESSION, True))
        self.assertIsNone(identity("not a uuid", 1))

    def test_mesh_counts_indices_and_coordinates(self):
        self.assertEqual(validate_mesh(mesh()), (3, 1))
        bad_index = mesh()[:-4] + struct.pack("<I", 3)
        bad_float = mesh()[:8] + struct.pack("<f", float("nan")) + mesh()[12:]
        for body in (b"", mesh()[:-1], bad_index, bad_float, struct.pack("<II", 600001, 0)):
            with self.assertRaises(ValueError):
                validate_mesh(body)


if __name__ == "__main__":
    unittest.main()
