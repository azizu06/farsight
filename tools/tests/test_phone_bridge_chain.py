"""Real APIs through two native PhoneBridgeServer instances, software only.

python -m unittest tools.tests.test_phone_bridge_chain -v
No devices, target host, phone capture, cellular or physical motors are used.
"""

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import aiohttp
from aiohttp import web

from tools.operator_relay import create_app as relay_app
from jetson.video import fragment_timing

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("swiftc"),
                     "Apple Network.framework and Swift compiler required")
class PhoneBridgeChainTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="farsight-native-chain-")
        cls.binary = Path(cls.build.name) / "chain"
        try:
            subprocess.run(["swiftc", str(ROOT / "ios/FarsightCapture/PhoneBridge.swift"),
                            str(ROOT / "ios/tests/PhoneBridgeChainFixture.swift"),
                            "-o", str(cls.binary)], check=True, capture_output=True, timeout=60)
        except BaseException:
            cls.build.cleanup()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    async def asyncSetUp(self):
        # Production event-loop behavior, rather than unittest's slow-callback debug mode.
        asyncio.get_running_loop().set_debug(False)
        self.work = tempfile.TemporaryDirectory(prefix="farsight-chain-api-")
        self.log = open(Path(self.work.name) / "services.log", "w+")
        self.backend = self.chain = self.relay = self.client = None
        self.sockets = []
        self.addAsyncCleanup(self.cleanup)
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0)); port = reserved.getsockname()[1]
        self.direct = f"http://127.0.0.1:{port}"
        args = [sys.executable, "-m", "jetson.server", "--demo", "--simulate-rover",
                "--host", "127.0.0.1", "--port", str(port), "--detector", "hog",
                "--mission-dir", self.work.name, "--tts-dir", self.work.name]
        self.h264 = bool(shutil.which("ffmpeg"))
        if self.h264:
            args.append("--h264")
        self.backend = subprocess.Popen(args, cwd=ROOT, stdout=self.log, stderr=self.log,
                                        start_new_session=True)
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.backend.poll() is not None:
                self.log.seek(0); self.fail("Backend exited: " + self.log.read()[-3000:])
            try:
                health = await self.get_json(self.direct + "/health")
                if health["camera"] and (not self.h264 or health["video"]["ready"]):
                    self.assertTrue(health["control"]["simulated"])
                    self.assertEqual(health["camera_source"], "SIMULATED")
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(.05)
        else:
            self.fail("Simulated backend readiness deadline")
        self.chain = await asyncio.create_subprocess_exec(str(self.binary), str(port),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=self.log)
        ready = await self.chain_reply()
        self.assertEqual(ready["action"], "ready")
        self.native_port = ready["operator"]["port"]
        self.native_url = f"http://127.0.0.1:{self.native_port}"
        await self.start_relay()

    async def start_relay(self):
        if self.relay:
            await self.relay.cleanup()
        self.relay = web.AppRunner(relay_app(self.native_url), shutdown_timeout=.2)
        await self.relay.setup()
        site = web.TCPSite(self.relay, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def chain_reply(self):
        line = await asyncio.wait_for(self.chain.stdout.readline(), 6)
        self.assertTrue(line, "Native fixture ended unexpectedly")
        return json.loads(line)

    async def chain_action(self, action):
        self.chain.stdin.write((action + "\n").encode())
        await self.chain.stdin.drain()
        result = await self.chain_reply()
        self.assertEqual(result["action"], action)
        return result

    async def get_json(self, url):
        async with self.client.get(url) as response:
            response.raise_for_status()
            return await response.json()

    async def until(self, operation, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = await operation()
            if predicate(value):
                return value
            await asyncio.sleep(.01)
        self.fail("API condition deadline; last=" + str(value))

    async def fresh_camera(self):
        sent = time.monotonic()
        async with self.client.get(self.url + "/camera.jpg") as response:
            response.raise_for_status()
            jpeg = await response.read()
            self.assertTrue(jpeg.startswith(b"\xff\xd8"))
            self.assertEqual(response.headers["X-Frame-Source"], "SIMULATED")
            self.assertIn("no-store", response.headers["Cache-Control"])
            age = float(response.headers["X-Frame-Age-Ms"]) + (time.monotonic() - sent) * 1000
        self.assertLess(age, 500, "Motion requires an actually received fresh simulated image")
        return age, time.monotonic()

    async def test_production_apis_video_and_simulated_stop_paths(self):
        health = await self.get_json(self.url + "/health")
        self.assertTrue(health["control"]["simulated"])
        self.assertEqual(health["source"], "SIMULATED")
        self.assertEqual(health["detector"], "hog")
        await self.fresh_camera()
        detections = await self.client.ws_connect(self.url + "/ws/detections")
        self.sockets.append(detections)
        packet = await self.until(lambda: asyncio.wait_for(detections.receive_json(), 3),
            lambda p: p["fresh"] and p["source"] == "SIMULATED", timeout=5)
        self.assertGreaterEqual(packet["frame"], 0)
        self.assertEqual(packet["detector"], "hog")
        self.assertIsInstance(packet["boxes"], list)
        await detections.close()

        if self.h264:
            video = await self.client.ws_connect(self.url + "/ws/video")
            self.sockets.append(video)
            init = await asyncio.wait_for(video.receive_json(), 3)
            self.assertEqual(init["type"], "video_init")
            self.assertEqual(init["source"], "SIMULATED")
            self.assertIn("avc1.", init["mime"])
            payload = await asyncio.wait_for(video.receive_bytes(), 3)
            self.assertIn(b"ftyp", payload); self.assertIn(b"moov", payload)
            previous_end, previous_frame = -1, -1
            for request in range(1, 7):
                await video.send_json({"type": "next", "request_id": request})
                metadata = await asyncio.wait_for(video.receive_json(), 3)
                payload = await asyncio.wait_for(video.receive_bytes(), 3)
                self.assertEqual(metadata["type"], "video_segment")
                self.assertEqual(metadata["request_id"], request)
                self.assertEqual(metadata["source"], "SIMULATED")
                self.assertIn(b"moof", payload); self.assertIn(b"mdat", payload)
                first, count, start, end = fragment_timing(payload, 10)
                self.assertGreaterEqual(start, previous_end - 1e-6)
                self.assertAlmostEqual(start, metadata["media_start_s"])
                self.assertAlmostEqual(end, metadata["media_end_s"])
                self.assertEqual(count, len(metadata["samples"]))
                self.assertGreaterEqual(metadata["frame"], previous_frame)
                self.assertEqual(metadata["frame"], metadata["samples"][-1]["frame"])
                self.assertGreaterEqual(metadata["age_ms"], 0)
                previous_end, previous_frame = end, metadata["frame"]
            await video.close()

        results = []
        for fault in ("release", "lease", "disconnect", "stop-operator", "stop-rover"):
            with self.subTest(fault=fault):
                ws = await self.client.ws_connect(self.url + "/ws/control")
                self.sockets.append(ws)
                status = await asyncio.wait_for(ws.receive_json(), 3)
                self.assertTrue(status["simulated"])
                self.assertEqual(status["permit_ms"], 250)
                session = status["session_id"]
                await ws.send_json(dict(session_id=session, seq=0, deadman=False))
                status = await self.until(lambda: asyncio.wait_for(ws.receive_json(), 2),
                    lambda p: p["seq"] == 0 and not p["requires_release"])
                camera_age, received = await self.fresh_camera()
                # Refresh permits after the real JPEG request; never recycle queued old tokens.
                status = await asyncio.wait_for(ws.receive_json(), 2)
                status = await asyncio.wait_for(ws.receive_json(), 2)
                age = camera_age + (time.monotonic() - received) * 1000
                self.assertLess(age, 500)
                await ws.send_json(dict(session_id=session, seq=1, permit=status["permit"],
                    deadman=True, v=.4, w=0, pan=0, mode="teleop",
                    video_fresh=True, video_age_ms=age))
                await self.until(lambda: self.get_json(self.direct + "/control/status"),
                    lambda p: p["seq"] == 1 and bool(p["left_pwm"] or p["right_pwm"]))
                triggered = time.monotonic()
                if fault == "release":
                    await ws.send_json(dict(session_id=session, seq=2, deadman=False))
                elif fault == "disconnect":
                    await ws.close()
                elif fault.startswith("stop-"):
                    stopped = await self.chain_action(fault)
                    name = fault.removeprefix("stop-")
                    self.assertFalse(stopped[name]["running"])
                    self.assertEqual(stopped[name]["active"], 0)
                    self.assertEqual(stopped[name]["buffered_bytes"], 0)
                # Lease scenario sends no renewal while the control socket remains open.
                stopped = await self.until(lambda: self.get_json(self.direct + "/control/status"),
                    lambda p: not p["left_pwm"] and not p["right_pwm"], timeout=1)
                elapsed_ms = (time.monotonic() - triggered) * 1000
                self.assertLess(elapsed_ms, 500)
                if fault == "lease":
                    self.assertEqual(stopped["reason"], "lease_expired")
                    self.assertTrue(stopped["requires_release"])
                if fault == "release":
                    self.assertEqual(stopped["reason"], "deadman_released")
                    self.assertFalse(stopped["requires_release"])
                await ws.close()
                if fault.startswith("stop-"):
                    ready = await self.chain_action("start")
                    self.native_url = f"http://127.0.0.1:{ready['operator']['port']}"
                    await self.start_relay()
                    recovered = await self.get_json(self.url + "/control/status")
                    self.assertEqual((recovered["left_pwm"], recovered["right_pwm"]), (0, 0))
                    self.assertTrue(recovered["requires_release"])
                results.append(dict(fault=fault, observed_stop_ms=round(elapsed_ms, 1), reason=stopped["reason"]))
        snapshot = await self.chain_action("snapshot")
        for name in ("operator", "rover"):
            self.assertLessEqual(snapshot[name]["peak_buffered_bytes"], 16 * 32768)
            self.assertGreater(snapshot[name]["forwarded_bytes"], 0)
        print("Software-only native bridge chain:", json.dumps({"h264": self.h264, "stops": results}))

    async def cleanup(self):
        for ws in self.sockets:
            with suppress(Exception):
                await asyncio.wait_for(ws.close(), .5)
        if self.client:
            await self.client.close()
        if self.relay:
            await self.relay.cleanup()
        if self.chain and self.chain.returncode is None:
            with suppress(Exception):
                await self.chain_action("quit")
            try:
                await asyncio.wait_for(self.chain.wait(), 2)
            except asyncio.TimeoutError:
                self.chain.kill(); await self.chain.wait()
        if self.backend and self.backend.poll() is None:
            os.killpg(self.backend.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(self.backend.wait, 5)
            except subprocess.TimeoutExpired:
                os.killpg(self.backend.pid, signal.SIGKILL)
                await asyncio.to_thread(self.backend.wait)
        self.log.close(); self.work.cleanup()


if __name__ == "__main__":
    unittest.main()
