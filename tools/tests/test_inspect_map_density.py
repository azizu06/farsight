import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from tools.inspect_map_density import endpoint_url, inspect


SESSION = "12345678-1234-4321-9876-123456789abc"
OTHER = "22345678-1234-4321-9876-123456789abc"


class DensityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.bodies = {
            "/map/status": {"source": "IPHONE LIDAR", "session_id": SESSION.upper(), "map_epoch": 4,
                            "points": 123456, "triangles": 456, "voxel_m": .01, "capped": True},
            "/geometry/status": {"session": SESSION.replace("-", ""), "epoch": 4,
                                 "retained": {"vertices": 54, "points": 54, "bytes": 999},
                                 "triangles": 18, "snapshot_complete": False},
            "/health": {"service": "mac-render", "stereo": False, "ready": True},
        }
        self.delays = {}
        self.responses = {}

        async def handle(request):
            self.requests.append((request.method, request.path, request.headers.get("Accept-Encoding")))
            await asyncio.sleep(self.delays.get(request.path, 0))
            if request.path in self.responses:
                response = self.responses[request.path](request)
                if isinstance(response, web.HTTPException):
                    raise response
                return response
            return web.json_response(self.bodies[request.path])

        self.app = web.Application()
        for route in self.bodies:
            self.app.router.add_get(route, handle)
        self.server = TestServer(self.app)
        await self.server.start_server()
        self.origin = str(self.server.make_url("/"))

    async def asyncTearDown(self):
        await self.server.close()

    async def run_hops(self, *names, **kwargs):
        return await inspect({name: self.origin for name in names}, **kwargs)

    async def test_actual_contract_fields_unknowns_and_get_only_routes(self):
        report = await self.run_hops("operator", "geometry", "renderer")
        self.assertTrue(report["all_endpoints_responded"])
        operator, geometry, renderer = [hop["evidence"] for hop in report["hops"]]
        self.assertEqual(operator["retained_unique_points"], {"value": 123456, "path": "points"})
        self.assertIsNone(operator["displayed_points"]["value"])
        self.assertIsNone(operator["points_cap"]["value"])
        self.assertEqual(geometry["retained_points"]["value"], 54)
        self.assertEqual(geometry["retained_vertices"]["value"], 54)
        self.assertIsNone(geometry["retained_unique_points"]["value"])
        self.assertIsNone(geometry["source"]["value"])
        self.assertIsNone(renderer["retained_points"]["value"])
        self.assertFalse(renderer["stereo"]["value"])
        self.assertEqual({finding["kind"] for finding in report["findings"]}, {"cap_reported", "partial_snapshot"})
        self.assertEqual(set(self.requests), {("GET", "/map/status", "identity"),
                                              ("GET", "/geometry/status", "identity"),
                                              ("GET", "/health", "identity")})
        self.assertEqual(report["hops"][0]["response"], self.bodies["/map/status"])
        self.assertEqual(report["hops"][0]["http_status"], 200)
        self.assertGreater(report["hops"][0]["response_bytes"], 0)
        self.assertTrue(report["hops"][0]["requested_at_utc"])

    async def test_arkit_mesh_points_are_vertices_not_unique_lidar(self):
        self.bodies["/map/status"].update(geometry_source="IPHONE ARKIT MESH",
                                           geometry={"matched": True, "snapshot_complete": True})
        report = await self.run_hops("operator")
        fields = report["hops"][0]["evidence"]
        self.assertEqual(fields["retained_vertices"]["value"], 123456)
        self.assertIsNone(fields["retained_unique_points"]["value"])
        self.assertIsNone(fields["retained_points"]["value"])
        self.assertEqual(fields["capture_source"]["value"], "IPHONE LIDAR")
        self.assertEqual(fields["source"]["value"], "IPHONE ARKIT MESH")

    async def test_disagreement_and_explicit_observations_display_and_caps(self):
        self.bodies["/geometry/status"].update(session=OTHER, epoch=5, source="IPHONE ARKIT MESH")
        self.bodies["/health"].update(session_id=SESSION, map_epoch=4, source="IPHONE LIDAR",
                                    point_observations=2300000, retained_unique_points=1700000,
                                    retained_points=1700000, displayed_points=1500000, points_cap=1500000)
        report = await self.run_hops("operator", "geometry", "renderer")
        kinds = [finding["kind"] for finding in report["findings"]]
        self.assertEqual(kinds.count("session_disagreement"), 2)
        self.assertEqual(kinds.count("epoch_disagreement"), 2)
        self.assertEqual(kinds.count("source_disagreement"), 2)
        self.assertIn("reported_count_exceeds_cap", kinds)
        fields = report["hops"][2]["evidence"]
        self.assertEqual(fields["point_observations"]["value"], 2300000)
        self.assertEqual(fields["retained_unique_points"]["value"], 1700000)
        self.assertEqual(fields["displayed_points"]["value"], 1500000)

    async def test_unknown_malformed_fields_are_never_guessed(self):
        self.bodies["/map/status"] = dict(points=True, map_epoch=True, source="", session_id=42,
                                           capped="true", triangles=-1, voxel_m=False, observations=999)
        report = await self.run_hops("operator")
        fields = report["hops"][0]["evidence"]
        for key in ("epoch", "session", "source", "capped", "retained_unique_points",
                    "retained_triangles", "voxel_m", "point_observations"):
            self.assertIsNone(fields[key]["value"], key)

    async def test_http_error_invalid_json_redirect_and_encoded_response(self):
        cases = [
            lambda request: web.Response(status=503, text="down"),
            lambda request: web.Response(body=b"not json"),
            lambda request: web.HTTPFound("/ws/control"),
            lambda request: web.Response(body=b"{}", headers={"Content-Encoding": "gzip"}),
            lambda request: web.Response(body=b"[1]"),
            lambda request: web.Response(body=b'{"points":NaN}'),
        ]
        for response in cases:
            self.responses["/map/status"] = response
            report = await self.run_hops("operator", "renderer")
            self.assertFalse(report["all_endpoints_responded"])
            self.assertFalse(report["hops"][0]["ok"])
            self.assertTrue(report["hops"][0]["error"])
            self.assertTrue(report["hops"][1]["ok"])
        self.assertTrue(all(path in ("/map/status", "/health") for _, path, _ in self.requests))

    async def test_content_length_and_chunked_size_bounds(self):
        self.responses["/map/status"] = lambda request: web.Response(body=b"x" * 33)
        report = await self.run_hops("operator", max_bytes=32)
        self.assertIn("byte limit", report["hops"][0]["error"])

        async def chunked(request):
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b"x" * 33)
            await response.write_eof()
            return response

        # Replace a response factory with an awaitable route through a second
        # real HTTP server, exercising a missing Content-Length.
        app = web.Application()
        app.router.add_get("/map/status", chunked)
        server = TestServer(app)
        await server.start_server()
        try:
            report = await inspect({"operator": str(server.make_url("/"))}, max_bytes=32)
            self.assertIn("byte limit", report["hops"][0]["error"])
            self.assertEqual(report["hops"][0]["response_bytes"], 33)
        finally:
            await server.close()

    async def test_overflow_exponents_in_raw_json_are_rejected(self):
        for body in (b'{"points":1e400}', b'{"other":{"nested":[-1e400]}}'):
            self.responses["/map/status"] = lambda request: web.Response(body=body)
            report = await self.run_hops("operator", "renderer")
            self.assertFalse(report["hops"][0]["ok"])
            self.assertIn("nonfinite JSON number", report["hops"][0]["error"])
            self.assertIsNone(report["hops"][0]["response"])
            self.assertTrue(report["hops"][1]["ok"])
            json.dumps(report, allow_nan=False)

    async def test_one_overall_timeout_preserves_fast_hop(self):
        self.delays = {"/map/status": .5, "/geometry/status": .5}
        started = time.monotonic()
        report = await self.run_hops("operator", "geometry", "renderer", timeout=.1)
        self.assertLess(time.monotonic() - started, .4)
        self.assertFalse(report["all_endpoints_responded"])
        self.assertFalse(report["hops"][0]["ok"])
        self.assertFalse(report["hops"][1]["ok"])
        self.assertTrue(report["hops"][2]["ok"])

    async def test_connection_failure_is_recorded(self):
        app = web.Application()
        server = TestServer(app)
        await server.start_server()
        origin = str(server.make_url("/"))
        await server.close()
        report = await inspect({"operator": origin}, timeout=.2)
        self.assertFalse(report["hops"][0]["ok"])
        self.assertIsNone(report["hops"][0]["http_status"])
        self.assertIn("ClientConnectorError", report["hops"][0]["error"])

    async def test_phone_explicit_contract(self):
        self.bodies["/geometry/status"] = dict(source="IPHONE ARKIT MESH", session_id=SESSION,
            map_epoch=4, vertices=300000, triangles=100000, anchors=80, dropped=7, ready=True)
        report = await self.run_hops("phone")
        fields = report["hops"][0]["evidence"]
        self.assertEqual(fields["retained_vertices"]["value"], 300000)
        self.assertIsNone(fields["retained_unique_points"]["value"])
        self.assertIsNone(fields["points_cap"]["value"])
        self.assertEqual(fields["dropped"]["value"], 7)


class CLITests(unittest.TestCase):
    def test_endpoint_allowlist(self):
        self.assertEqual(endpoint_url("operator", "http://localhost:8080/"), "http://localhost:8080/map/status")
        self.assertEqual(endpoint_url("renderer", "http://localhost:8090/health"), "http://localhost:8090/health")
        for url in ("ftp://localhost", "http://a/ws/control", "http://a/map/reset", "http://user:pass@a",
                    "http://a/map/status?reset=1", "http://a/#fragment", "http://a:bad"):
            with self.assertRaises(ValueError):
                endpoint_url("operator", url)

    def test_cli_emits_json_even_when_server_unreachable(self):
        root = Path(__file__).resolve().parents[2]
        completed = subprocess.run([sys.executable, str(root / "tools/inspect_map_density.py"),
                                    "--operator", "http://127.0.0.1:1", "--timeout", ".2"],
                                   capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual([hop["name"] for hop in report["hops"]], ["operator"])


if __name__ == "__main__":
    unittest.main()
