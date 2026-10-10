"""Real TCP/subprocess integration tests, including a congested video consumer."""

import asyncio
from contextlib import suppress
import json
from pathlib import Path
import socket
import ssl
import shutil
import subprocess
import sys
import tempfile
import unittest

import aiohttp
from aiohttp import web

from jetson.mapping.compress import encode, negotiate
from tools.operator_relay import local_url, upstream_origin

ROOT = Path(__file__).resolve().parents[2]


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dist = Path(self.temp.name)
        (self.dist / "index.html").write_text("<html>offline Quest</html>")
        (self.dist / "asset.js").write_text("console.log('bundled')")
        self.commands = []
        self.control_disconnect = asyncio.Event()
        self.video_disconnect = asyncio.Event()
        self.upstream = web.Application()

        self.map_body = bytes(range(256)) * 400
        self.seen_encoding = {}

        async def health(request):
            self.seen_encoding["health"] = request.headers.get("Accept-Encoding")
            return web.json_response({"camera": True, "audio_url": self.origin + "/tts/42.wav",
                                      "reference": "https://example.com/independent"})

        async def points(request):
            self.seen_encoding["points"] = request.headers.get("Accept-Encoding")
            coding = negotiate(request.headers.get("Accept-Encoding"))
            headers = {"Vary": "Accept-Encoding"}
            body = self.map_body
            if coding:
                body, headers["Content-Encoding"] = encode(coding, body), coding
            return web.Response(body=body, headers=headers, content_type="application/octet-stream")

        async def ask(request):
            body = await request.json()
            return web.json_response({"answer": body["text"], "audio_url": self.origin + "/tts/42.wav"})

        async def image(request):
            return web.Response(body=b"jpeg", headers={"Content-Type": "image/jpeg", "X-Frame-Id": "42"})

        async def stream(request):
            response = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=frame"})
            await response.prepare(request)
            await response.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\njpeg\r\n")
            with suppress(ConnectionError):
                while True:
                    await asyncio.sleep(0.05)
                    await response.write(b"--frame\r\nContent-Type: image/jpeg\r\n\r\njpeg\r\n")
            return response

        async def control(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"session_id": "fresh-session", "permit": "expiring", "type": "permit"})
            try:
                async for message in ws:
                    data = json.loads(message.data)
                    self.commands.append(data)
                    if data.get("close"):
                        await ws.close()
                    else:
                        await ws.send_json({"seq": data.get("seq"), "ack": True})
            finally:
                self.control_disconnect.set()
            return ws

        async def video(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            try:
                while True:
                    await ws.send_bytes(b"x" * 128000)
            except (ConnectionError, RuntimeError):
                pass
            finally:
                self.video_disconnect.set()
            return ws

        async def redirect(request):
            raise web.HTTPFound(self.origin + "/camera.jpg")

        async def foreign_redirect(request):
            raise web.HTTPFound("https://example.com/escape")

        self.upstream.router.add_get("/health", health)
        self.upstream.router.add_get("/control/status", health)
        self.upstream.router.add_post("/ask", ask)
        self.upstream.router.add_get("/map/points.bin", points)
        self.upstream.router.add_get("/camera.jpg", image)
        self.upstream.router.add_get("/stream.mjpg", stream)
        self.upstream.router.add_get("/ws/control", control)
        self.upstream.router.add_get("/ws/video", video)
        self.upstream.router.add_get("/map/redirect", redirect)
        self.upstream.router.add_get("/map/foreign", foreign_redirect)
        self.runner = web.AppRunner(self.upstream, shutdown_timeout=0.1)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.origin = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.log = open(self.dist / "relay.log", "w+")
        self.process = subprocess.Popen([sys.executable, "-m", "tools.operator_relay", "--upstream", self.origin,
            "--port", str(port), "--dist", str(self.dist)], cwd=ROOT, stdout=self.log, stderr=self.log)
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        for _ in range(100):
            try:
                async with self.client.get(self.base + "/relay/health") as response:
                    if response.status == 200:
                        break
            except aiohttp.ClientError:
                await asyncio.sleep(0.02)
        else:
            self.log.seek(0)
            self.fail("Relay failed to start: " + self.log.read())

    async def asyncTearDown(self):
        await self.client.close()
        self.process.terminate()
        await asyncio.to_thread(self.process.wait, 3)
        await self.runner.cleanup()
        self.log.close()
        self.temp.cleanup()

    async def test_offline_static_json_urls_and_frame_headers(self):
        async with self.client.get(self.base + "/") as response:
            self.assertIn("offline Quest", await response.text())
        async with self.client.get(self.base + "/asset.js") as response:
            self.assertIn("bundled", await response.text())
        async with self.client.get(self.base + "/health") as response:
            result = await response.json()
            self.assertEqual(result["audio_url"], "/tts/42.wav")
            self.assertEqual(result["reference"], "https://example.com/independent")
        async with self.client.get(self.base + "/control/status") as response:
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())["camera"])
        async with self.client.post(self.base + "/ask", json={"text": "hello"}) as response:
            self.assertEqual((await response.json())["answer"], "hello")
        async with self.client.get(self.base + "/camera.jpg") as response:
            self.assertEqual(response.headers["X-Frame-Id"], "42")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(await response.read(), b"jpeg")

    async def test_map_compression_passes_through_byte_identical(self):
        # Raw client: no auto-decompress, so the bytes seen are the bytes relayed.
        async with aiohttp.ClientSession(auto_decompress=False) as raw:
            async with raw.get(self.base + "/map/points.bin", headers={"Accept-Encoding": "gzip"}) as response:
                body = await response.read()
                self.assertEqual(response.headers["Content-Encoding"], "gzip")
                self.assertEqual(response.headers["Vary"], "Accept-Encoding")
            self.assertEqual(self.seen_encoding["points"], "gzip")
            self.assertEqual(body, encode("gzip", self.map_body))
            self.assertLess(len(body), len(self.map_body) / 4)
            async with raw.get(self.base + "/map/points.bin", headers={"Accept-Encoding": "identity"}) as response:
                self.assertNotIn("Content-Encoding", response.headers)
                self.assertEqual(await response.read(), self.map_body)
        # Decoding client (aiohttp default) still gets the original map.
        async with self.client.get(self.base + "/map/points.bin") as response:
            self.assertIn(response.headers["Content-Encoding"], ("gzip", "br"))
            self.assertEqual(await response.read(), self.map_body)

    async def test_json_stays_identity_upstream_even_if_client_accepts_gzip(self):
        async with self.client.get(self.base + "/health", headers={"Accept-Encoding": "br, gzip"}) as response:
            self.assertEqual((await response.json())["audio_url"], "/tts/42.wav")
            self.assertNotIn("Content-Encoding", response.headers)
        self.assertEqual(self.seen_encoding["health"], "identity")

    async def test_mjpeg_streams_without_waiting_for_end(self):
        async with self.client.get(self.base + "/stream.mjpg") as response:
            data = await asyncio.wait_for(response.content.read(100), 1)
            self.assertIn(b"jpeg", data)
            self.assertEqual(response.headers["Content-Type"], "multipart/x-mixed-replace; boundary=frame")

    async def test_control_roundtrip_stale_stop_and_no_replay(self):
        ws = await self.client.ws_connect(self.base + "/ws/control")
        self.assertEqual((await ws.receive_json())["permit"], "expiring")
        command = {"seq": 1, "session_id": "fresh-session", "permit": "expiring", "deadman": True,
                   "v": 0.4, "video_fresh": True, "video_age_ms": 100}
        await ws.send_json(command)
        self.assertEqual((await ws.receive_json())["seq"], 1)
        self.assertEqual(self.commands[0], command)
        await ws.send_json({**command, "seq": 2, "video_age_ms": 501})
        await ws.receive_json()
        self.assertFalse(self.commands[1]["deadman"])
        self.assertEqual(self.commands[1]["v"], 0)
        await ws.close()
        await asyncio.wait_for(self.control_disconnect.wait(), 1)
        ws = await self.client.ws_connect(self.base + "/ws/control")
        await ws.receive_json()
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.commands), 2, "Reconnect must never replay old commands")
        await ws.send_json({"deadman": False, "close": True})
        self.assertIn((await ws.receive()).type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED))
        await ws.close()

    async def test_video_pressure_keeps_control_independent(self):
        video = await self.client.ws_connect(self.base + "/ws/video")
        # Do not consume video: its TCP path stalls and is severed after bounded sends.
        control = await self.client.ws_connect(self.base + "/ws/control")
        await control.receive_json()
        try:
            await asyncio.sleep(0.1)
            await control.send_json({"seq": 12, "deadman": False})
            self.assertEqual((await asyncio.wait_for(control.receive_json(), 1))["seq"], 12)
            await asyncio.wait_for(self.video_disconnect.wait(), 3)
        finally:
            await control.close()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(video.close(), 0.25)

    async def test_redirects_stay_on_usb_and_foreign_origins_rejected(self):
        async with self.client.get(self.base + "/map/redirect", allow_redirects=False) as response:
            self.assertEqual(response.headers["Location"], "/camera.jpg")
        async with self.client.get(self.base + "/map/foreign", allow_redirects=False) as response:
            self.assertEqual(response.status, 502)
        async with self.client.get(self.base + "/health", headers={"Origin": "https://evil.example"}) as response:
            self.assertEqual(response.status, 403)
        async with self.client.get(self.base + "/health", headers={"Host": "evil.example"}) as response:
            self.assertEqual(response.status, 403)

    async def test_qualification_command_sends_only_stops(self):
        result = await asyncio.to_thread(subprocess.run,
            [sys.executable, "-m", "tools.qualify_link", "--url", self.base, "--seconds", "0.4"],
            cwd=ROOT, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        self.assertTrue(report["transport_checks_passed"])
        self.assertTrue(report["measurement_only"])
        self.assertGreater(report["video_bytes"], 0)
        self.assertGreater(len(self.commands), 0)
        self.assertTrue(all(command["deadman"] is False for command in self.commands))

    @unittest.skipUnless(shutil.which("openssl"), "TLS fixture needs openssl")
    async def test_https_rejects_untrusted_certificate_and_accepts_configured_ca(self):
        cert, key = self.dist / "cert.pem", self.dist / "key.pem"
        await asyncio.to_thread(subprocess.run, ["openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
            check=True, capture_output=True, timeout=10)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        secure_site = web.TCPSite(self.runner, "127.0.0.1", 0, ssl_context=context)
        await secure_site.start()
        secure_origin = f"https://127.0.0.1:{secure_site._server.sockets[0].getsockname()[1]}"

        async def restart(ca=False):
            self.process.terminate()
            await asyncio.to_thread(self.process.wait, 3)
            port = int(self.base.rsplit(":", 1)[1])
            command = [sys.executable, "-m", "tools.operator_relay", "--upstream", secure_origin,
                       "--port", str(port), "--dist", str(self.dist)]
            if ca:
                command += ["--ca-file", str(cert)]
            self.process = subprocess.Popen(command, cwd=ROOT, stdout=self.log, stderr=self.log)
            for _ in range(100):
                try:
                    async with self.client.get(self.base + "/relay/health") as response:
                        if response.status == 200:
                            return
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.02)
            self.fail("TLS relay did not start")

        await restart()
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        def expected_handshake_reset(loop, context):
            if isinstance(context.get("exception"), ConnectionResetError):
                return
            if previous_handler:
                previous_handler(loop, context)
            else:
                loop.default_exception_handler(context)
        loop.set_exception_handler(expected_handshake_reset)
        try:
            async with self.client.get(self.base + "/health") as response:
                self.assertEqual(response.status, 502)
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)
        await restart(ca=True)
        async with self.client.get(self.base + "/health") as response:
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())["camera"])

    def test_url_validation(self):
        for value in ("https://jetson/path", "file:///etc/passwd", "http://user:secret@jetson", "http://jetson?x=1"):
            with self.assertRaises(ValueError):
                upstream_origin(value)
        origin = upstream_origin("https://jetson:8443")
        self.assertEqual(local_url("https://jetson:8443/tts/a.wav?x=1", origin), "/tts/a.wav?x=1")
        self.assertEqual(local_url("https://jetson.evil:8443/tts/a.wav", origin), "https://jetson.evil:8443/tts/a.wav")


if __name__ == "__main__":
    unittest.main()
