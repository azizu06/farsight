import asyncio
import struct
import unittest
from unittest import mock
import uuid

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import numpy as np

from jetson.mapping.dense import DenseMap, MAX_RECORDS, unpack
from tools.dense_geometry import HEADER, GeometryConsumer, decode_geometry
from tools.dense_map import MacCloud, DenseAccumulator, mount
from tools.operator_relay import create_app


def geometry_record(op, seq, anchor=b"a" * 16, xyz=None):
    xyz = np.array([[.1, .2, -.3], [.2, .2, -.3], [.1, .3, -.3]] if xyz is None else xyz, "<f4")
    if op != 0:
        xyz = np.empty((0, 3), "<f4")
    indices = [0, 1, 2] if op == 0 else []
    return HEADER.pack(1, op, 0, seq, 4, uuid.UUID(int=1).bytes, anchor,
                       *np.eye(4).flatten(order="F"), len(xyz), len(indices)) + xyz.tobytes() + struct.pack("<" + "I" * len(indices), *indices)


class CloudTests(unittest.TestCase):
    def test_retained_rgbd_survives_cache_expiry_and_same_epoch_bootstrap(self):
        now = [0.]
        accumulator = DenseAccumulator("http://localhost:8443")
        accumulator.cloud = MacCloud(retained_history=True, clock=lambda: now[0])
        metadata = dict(epoch="source-a", reset=True, source=["simulated-room", 1],
                        points=1, cursor=1, snapshot=False)
        accumulator.accept(metadata, [(10, 0, 0, 1, 2, 3, 1)])
        epoch = accumulator.cloud.epoch
        now[0] = 31.
        accumulator.accept(dict(metadata, reset=False, points=0, cursor=2), [(10, 0, 0, 0, 0, 0, 0)])
        self.assertEqual(len(accumulator.cloud.cells), 1)
        accumulator.accept(dict(metadata, cursor=3), [(20, 0, 0, 4, 5, 6, 1)])
        self.assertEqual(accumulator.cloud.epoch, epoch)
        self.assertEqual(len(accumulator.cloud.cells), 2)
        now[0] = 90000.
        manifest = accumulator.cloud.manifest([0, 0, 0])
        self.assertEqual(manifest["points"], 2)
        self.assertTrue(manifest["retained_history"])
        wire, rows = unpack(accumulator.cloud.delta())
        self.assertTrue(wire["retained_history"])
        self.assertEqual(len(rows), 2)
        # A true capture/source epoch change removes the old room.
        accumulator.accept(dict(metadata, epoch="source-b"), [(30, 0, 0, 7, 8, 9, 1)])
        self.assertEqual(list(accumulator.cloud.cells), [(3, 0, 0)])
        self.assertNotEqual(accumulator.cloud.epoch, epoch)
        accumulator.accept(dict(metadata, epoch="source-b", source=["simulated-room", 2]),
                           [(40, 0, 0, 10, 11, 12, 1)])
        self.assertEqual(list(accumulator.cloud.cells), [(4, 0, 0)])

    def test_person_footprint_fade_is_off_by_default(self):
        now = [0.]
        cloud = MacCloud(retained_history=True, clock=lambda: now[0])
        cloud.apply([(1000, 1000, -2000, 9, 9, 9, 1)])
        cloud.observe_people([(1.0, 1.0, -2.0)])
        now[0] = 30.
        self.assertEqual(cloud.fade_footprints(), 0)
        self.assertEqual(len(cloud.cells), 1)

    @mock.patch("tools.dense_map.FADE_FOOTPRINTS", True)
    def test_person_footprint_fades_unobserved_cells_and_keeps_the_rest(self):
        now = [0.]
        cloud = MacCloud(retained_history=True, clock=lambda: now[0])
        # Person torso at (1, 1, -2) m; body samples at 0.3-1.6 m height, floor at 0, wall 1 m away.
        body = [(1000 + dx, y, -2000 + dz, 9, 9, 9, 1) for dx in (-100, 0, 100) for dz in (-100, 0, 100)
                for y in (300, 1000, 1600)]
        cloud.apply(body + [(1000, 0, -2000, 1, 1, 1, 1), (2000, 1000, -2000, 2, 2, 2, 1)])
        cloud.observe_people([(1.0, 1.0, -2.0)])
        now[0] = 10.
        self.assertEqual(cloud.fade_footprints(), 0)  # observed recently: kept
        now[0] = 20.
        cloud.apply([body[0]])  # this body cell is still being observed
        now[0] = 30.
        before = cloud.revision
        self.assertEqual(cloud.fade_footprints(), len(body) - 1)
        self.assertEqual(set(cloud.cells), {tuple(v // 10 for v in body[0][:3]), (100, 0, -200), (200, 100, -200)})
        # Deletes reach Mac clients through the journal.
        _, rows = unpack(cloud.delta(cloud.epoch, before))
        self.assertEqual(len(rows), len(body) - 1)
        self.assertTrue(all(row[6] == 0 for row in rows))
        # Sightings older than the memory window no longer fade anything.
        cloud.apply(body)
        now[0] = 30. + 61 + 16
        self.assertEqual(cloud.fade_footprints(), 0)

    def test_history_capacity_evicts_and_bootstrap_snapshots_still_expire(self):
        now = [0.]
        cloud = MacCloud(retained_history=True, clock=lambda: now[0])
        cloud.capacity = 2
        cloud.apply([(x * 10, 0, 0, 1, 2, 3, 1) for x in range(3)])
        self.assertEqual(list(cloud.cells), [(1, 0, 0), (2, 0, 0)])
        metadata, _ = unpack(cloud.delta(limit=1))
        self.assertTrue(cloud.snapshots)
        now[0] = 31.
        cloud.expire()
        self.assertFalse(cloud.snapshots)
        self.assertEqual(len(cloud.cells), 2)

    def test_mac_retains_multiple_jetson_caches_and_bounded_lod_real_samples(self):
        jetson = DenseMap(capacity=10, cache_only=True)
        mac = MacCloud()
        jetson.apply([(i * 10, 0, 0, 1, 20, 30, 1) for i in range(100)])
        _, records = unpack(jetson.delta(jetson.epoch, 0))
        mac.apply(records)
        self.assertEqual(len(jetson.cells), 10)
        self.assertEqual(len(mac.cells), 100)
        # A crowded chunk is reduced by selecting observed samples at coarser LOD.
        mac.apply([(x * 10, y * 10, 10, 1, 2, 3, 1) for x in range(120) for y in range(120)])
        manifest = mac.manifest([0, 0, 0])
        meta, points = unpack(mac.view_chunk((0, 0, 0), 0))
        self.assertLessEqual(len(points), MAX_RECORDS)
        self.assertGreater(meta["sample_voxel_m"], .01)
        self.assertTrue(all(tuple(v // 10 for v in row[:3]) in mac.cells for row in points))
        self.assertLessEqual(len(manifest["chunks"]), 32)
        mac.apply([(0, 0, 0, 0, 0, 0, 0)])
        _, points = unpack(mac.view_chunk((0, 0, 0), 0))
        self.assertFalse(any(row[:3] == (0, 0, 0) for row in points))

    def test_gray_anchor_replace_remove_shared_cells_and_bootstrap(self):
        cloud = MacCloud(ttl=86400)
        consumer = GeometryConsumer(cloud)
        # A bootstrap reset can have a newer producer seq than retained anchors.
        consumer.apply(decode_geometry(geometry_record(2, 10) + geometry_record(0, 2)), bootstrap=True)
        self.assertEqual(len(cloud.cells), 3)
        consumer.apply(decode_geometry(geometry_record(0, 11, anchor=b"b" * 16)))
        consumer.apply(decode_geometry(geometry_record(4, 12)))
        self.assertEqual(len(cloud.cells), 3)  # memory eviction retains Mac geometry
        consumer.apply(decode_geometry(geometry_record(1, 13)))
        self.assertEqual(len(cloud.cells), 3)
        consumer.apply(decode_geometry(geometry_record(1, 14, anchor=b"b" * 16)))
        self.assertEqual(len(cloud.cells), 0)
        with self.assertRaises(ValueError):
            decode_geometry(geometry_record(0, 14)[:-1])

    def test_same_sequence_native_fragments_and_complete_bootstrap(self):
        cloud = MacCloud(ttl=86400)
        consumer = GeometryConsumer(cloud)
        xyz = [[.4, .2, -.3], [.5, .2, -.3], [.4, .3, -.3]]
        body = geometry_record(2, 0) + geometry_record(0, 7) + geometry_record(0, 7, xyz=xyz)
        body += geometry_record(0, 7, anchor=b"b" * 16, xyz=[[1, 1, 1], [2, 1, 1], [1, 2, 1]])
        updates = decode_geometry(body)
        self.assertEqual(len(updates), 3)  # reset + merged fragments + distinct anchor
        consumer.apply(updates)
        self.assertEqual(len(cloud.cells), 9)
        self.assertEqual(consumer.after, 7)
        epoch = cloud.epoch
        consumer.apply([])  # caught-up empty response never resets retained geometry
        self.assertEqual(cloud.epoch, epoch)
        consumer.apply(decode_geometry(geometry_record(1, 8)))
        self.assertEqual(len(cloud.cells), 3)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_brotli_pull_same_origin_chunks_metrics_and_reset(self):
        from jetson.mapping.compress import encode
        jetson = DenseMap(cache_only=True)
        jetson.apply([(i * 10, 0, 0, 4, 5, 6, 1) for i in range(100)])
        async def delta(request):
            query = request.query
            body = jetson.delta(query.get("epoch"), int(query.get("after", 0)), query.get("snapshot") == "true",
                                anchor=int(query["anchor"]) if "anchor" in query else None)
            return web.Response(body=encode("br", body), headers={"Content-Encoding": "br"})
        upstream = web.Application(); upstream.router.add_get("/map/dense.bin", delta)
        server = TestServer(upstream); await server.start_server()
        relay = create_app(str(server.make_url("/")))
        accumulator = mount(relay, str(server.make_url("/")))
        client = TestClient(TestServer(relay)); await client.start_server()
        try:
            async with asyncio.timeout(3):
                while len(accumulator.cloud.cells) != 100:
                    await asyncio.sleep(.01)
            response = await client.get("/dense/manifest?x=0&y=0&z=0")
            manifest = await response.json()
            chunk = manifest["chunks"][0]
            response = await client.get(f"/dense/chunk.bin?id={chunk['id']}&lod={chunk['lod']}")
            metadata, rows = unpack(await response.read())
            self.assertEqual(metadata["epoch"], manifest["epoch"])
            self.assertEqual(len(rows), 100)
            await client.post("/dense/metrics", json=dict(points=100, fps=72, xr=True))
            status = await (await client.get("/dense/status")).json()
            self.assertEqual((status["jetson_points"], status["mac_points"], status["quest"]["points"]), (100, 100, 100))
            self.assertGreater(status["tailnet_bytes"], 0)
            old = accumulator.cloud.epoch
            jetson.clear()
            async with asyncio.timeout(3):
                while accumulator.cloud.epoch == old:
                    await asyncio.sleep(.01)
            self.assertFalse(accumulator.cloud.cells)
            response = await client.get("/dense/manifest?x=nan")
            self.assertEqual(response.status, 400)
        finally:
            await client.close(); await server.close()


if __name__ == "__main__":
    unittest.main()
