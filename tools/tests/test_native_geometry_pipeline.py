"""Native Swift producer -> real Rust assembler -> Python relay -> Quest codec.

Synthetic triangle/Jetson metadata, actual loopback protocols. No phone/cellular
or physical motion. Build rust/farsight-geometry before running this check.
"""
import asyncio
from contextlib import suppress
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import tempfile
import unittest

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer
from tools.operator_relay import create_app
from tools.qualify_geometry import qualify

ROOT = Path(__file__).resolve().parents[2]
BINARY = ROOT / "rust/target/debug/farsight-geometry"
SID = "123e4567-e89b-12d3-a456-426614174000"


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("swiftc") and BINARY.exists(),
                     "Mac native frameworks and built Rust assembler required")
class NativeGeometryPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_records_reach_quest_decoder_through_the_real_assembler_and_relay(self):
        await self.pipeline(through_operator=False)

    async def test_native_operator_forwarding_reaches_the_real_assembler_relay_and_quest(self):
        await self.pipeline(through_operator=True)

    async def pipeline(self, *, through_operator):
        with tempfile.TemporaryDirectory(prefix="farsight-native-geometry-pipeline-") as work:
            path = Path(work)
            binary = path / "producer"
            result = await asyncio.to_thread(subprocess.run, ["swiftc", *[str(ROOT / file) for file in (
                "ios/FarsightCapture/GeometryWire.swift", "ios/FarsightCapture/PhoneGeometryFeed.swift",
                "ios/FarsightCapture/PhoneGeometryServer.swift", "ios/FarsightCapture/PhoneBridge.swift",
                "ios/tests/NativeGeometrySourceFixture.swift")],
                "-o", str(binary)], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            producer = await asyncio.create_subprocess_exec(str(binary), *(["--through-operator"] if through_operator else []),
                                                           stdout=asyncio.subprocess.PIPE,
                                                           stderr=asyncio.subprocess.DEVNULL)
            assembler = None
            upstream = client = None
            try:
                line = await asyncio.wait_for(producer.stdout.readline(), 10)
                self.assertTrue(line.startswith(b"PORT="), line)
                phone_port = int(line.decode().strip().split("=")[1])
                route_line = (await asyncio.wait_for(producer.stdout.readline(), 3)).decode().strip()
                route, source = route_line.split(" SOURCE_PORT=")
                self.assertEqual(route, "ROUTE=operator-forwarded" if through_operator else "ROUTE=direct")
                if through_operator:
                    self.assertNotEqual(phone_port, int(source))
                else:
                    self.assertEqual(phone_port, int(source))
                reservation = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
                assembler_port = reservation.sockets[0].getsockname()[1]
                reservation.close(); await reservation.wait_closed()
                assembler = await asyncio.create_subprocess_exec(str(BINARY), "--listen", f"127.0.0.1:{assembler_port}",
                    "--upstream", f"ws://127.0.0.1:{phone_port}/geometry/stream", stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL)
                origin = f"http://127.0.0.1:{assembler_port}"
                async with ClientSession(timeout=ClientTimeout(total=1)) as http:
                    ready_deadline = asyncio.get_running_loop().time() + 8
                    for _ in range(150):
                        if asyncio.get_running_loop().time() >= ready_deadline:
                            self.fail("native assembler startup exceeded deadline")
                        try:
                            async with http.get(origin + "/geometry/status") as response:
                                status = await response.json()
                            if status.get("triangles") == 1 and status.get("snapshot_complete") is True:
                                break
                        except Exception:
                            pass
                        await asyncio.sleep(.05)
                    else:
                        self.fail("native stream did not publish a complete mesh through Rust")
                meta = dict(type="status", source="SIMULATED", device="Synthetic native pipeline",
                    session_id=SID.upper(), map_epoch=1, generation=1, revision=0, mesh_revision=0,
                    points=0, triangles=0, connected=True, fresh=True, tracking="normal", paused=False,
                    frames=1, rejected=0, dropped=0, processing_ms=0, age_ms=0, position=[0, 0, 0],
                    transform=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1], depth_range=[.5, 3], reason=None)
                jetson = web.Application()
                async def map_status(_): return web.json_response(meta)
                jetson.router.add_get("/map/status", map_status)
                upstream = TestServer(jetson); await upstream.start_server()
                client = TestClient(TestServer(create_app(str(upstream.make_url("/")), geometry_upstream=origin)))
                await client.start_server()
                for _ in range(100):
                    merged = await (await client.get("/map/status")).json()
                    if merged.get("triangles") == 1: break
                    await asyncio.sleep(.05)
                else: self.fail("relay did not match and publish native capture UUID/epoch")
                self.assertEqual(merged["source"], "SIMULATED")
                self.assertEqual(merged["geometry_source"], "IPHONE ARKIT MESH")
                # Read-only source-path verdict over the real native/Rust/relay
                # chain, still with synthetic triangle and Jetson pose metadata.
                verdict = await qualify(str(client.server.make_url("/")), timeout=3)
                self.assertTrue(verdict["qualified"], verdict)
                self.assertEqual(verdict["evidence"]["mesh"]["session_id"], SID)
                self.assertEqual(verdict["evidence"]["mesh"]["triangles"], 1)
                response = await client.get("/map/mesh.bin")
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["X-Map-Session"], SID)
                mesh = await response.read()
                self.assertEqual(struct.unpack_from("<II", mesh), (3, 3))
                expected = [(1, 2, 3), (2, 2, 3), (1, 3, 3)]
                for i, xyz in enumerate(expected): self.assertEqual(struct.unpack_from("<3f", mesh, 8 + i * 16), xyz)
                self.assertEqual(struct.unpack_from("<3I", mesh, 56), (0, 1, 2))
                (path / "mesh.bin").write_bytes(mesh)
                points = await (await client.get("/map/points.bin")).read()
                self.assertEqual(struct.unpack_from("<I", points)[0], 3)
                (path / "points.bin").write_bytes(points)
                script = """import{readFileSync}from'node:fs';import{decodeMesh,decodePoints}from'./quest/src/protocol.js';
const b=p=>{const x=readFileSync(p);return x.buffer.slice(x.byteOffset,x.byteOffset+x.byteLength)};
const m=decodeMesh(b(process.argv[1])),p=decodePoints(b(process.argv[2]));
if(m.count!==3||m.indices.length!==3||p.count!==3||m.positions[0]!==1||m.positions[1]!==2||m.positions[2]!==3)process.exit(1);
console.log('PASS native phone-code -> Rust assembly -> Python relay -> Quest production decoder');"""
                decoded = await asyncio.to_thread(subprocess.run, ["node", "--input-type=module", "-e", script,
                    str(path / "mesh.bin"), str(path / "points.bin")], cwd=ROOT, capture_output=True, text=True, timeout=10)
                self.assertEqual(decoded.returncode, 0, decoded.stdout + decoded.stderr)
                self.assertIn("PASS native phone-code", decoded.stdout)
            finally:
                async def stop_process(process):
                    if process.returncode is not None:
                        return
                    with suppress(ProcessLookupError):
                        process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except asyncio.TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await asyncio.wait_for(process.wait(), 5)
                cleanup = [stop_process(process) for process in (assembler, producer) if process]
                if client:
                    cleanup.append(asyncio.wait_for(client.close(), 5))
                if upstream:
                    cleanup.append(asyncio.wait_for(upstream.close(), 5))
                # A stalled HTTP close must not skip terminating either child.
                results = await asyncio.gather(*cleanup, return_exceptions=True)
                self.assertFalse([result for result in results if isinstance(result, BaseException)],
                                 "native pipeline cleanup failed")
