"""Assembled opt-in transport, using simulated outputs and real socket lifecycle."""

import asyncio
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import aiohttp

ROOT = Path(__file__).resolve().parents[3]


class BondedTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        self.origin = f"http://127.0.0.1:{port}"
        self.work = tempfile.TemporaryDirectory(prefix="farsight-bonded-transport-")
        self.log = open(Path(self.work.name) / "backend.log", "w+")
        self.process = subprocess.Popen([sys.executable, "-m", "jetson.server",
            "--demo", "--simulate-rover", "--detector", "none", "--bonded-control",
            "--host", "127.0.0.1", "--port", str(port),
            "--mission-dir", self.work.name, "--tts-dir", self.work.name],
            cwd=ROOT, stdout=self.log, stderr=self.log)
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2))
        self.addAsyncCleanup(self.cleanup)
        for _ in range(100):
            if self.process.poll() is not None:
                self.log.seek(0)
                self.fail("Backend exited: " + self.log.read()[-2000:])
            try:
                h = await self.health()
                if h["camera"] and h["rover"]:
                    self.assertTrue(h["control"]["simulated"])
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(.03)
        self.fail("Simulated backend readiness deadline")

    async def cleanup(self):
        await self.client.close()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait, 2)
        self.log.close()
        self.work.cleanup()

    async def health(self):
        async with self.client.get(self.origin + "/health") as response:
            response.raise_for_status()
            return await response.json()

    async def attach(self, secret=None):
        ws = await self.client.ws_connect(self.origin + "/ws/control/bonded")
        packet = {"type": "attach", "version": 1}
        if secret is not None:
            packet["secret"] = secret
        await ws.send_json(packet)
        attached = await asyncio.wait_for(ws.receive_json(), 2)
        self.assertEqual(attached["type"], "attached")
        self.assertEqual(attached["version"], 1)
        self.assertEqual(len(attached["secret"]), 64)
        return ws, attached

    async def status(self, ws, predicate=lambda s: True):
        deadline = asyncio.get_running_loop().time() + 2
        while True:
            packet = await asyncio.wait_for(ws.receive_json(),
                max(.001, deadline - asyncio.get_running_loop().time()))
            self.assertIn("status_seq", packet)
            self.assertNotIn("secret", packet)
            self.assertNotIn("path_id", packet)
            if predicate(packet):
                return packet

    def command(self, status, seq, held=True):
        return dict(session_id=status["session_id"], permit=status.get("permit"), seq=seq,
            deadman=held, v=.35 if held else 0, w=0, pan=0, video_fresh=True,
            video_age_ms=0)

    async def test_two_real_paths_share_owner_and_one_loss_does_not_stop(self):
        primary, first = await self.attach()
        secondary, second = await self.attach(first["secret"])
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertNotEqual(first["path_id"], second["path_id"])
        s = await self.status(secondary)
        release = self.command(s, 0, False)
        await primary.send_json(release)
        s = await self.status(secondary, lambda p: p["seq"] == 0)
        motion = self.command(s, 1)
        await primary.send_json(motion)
        await secondary.send_json(motion)
        s = await self.status(secondary, lambda p: p["seq"] == 1)
        self.assertEqual((s["left_pwm"], s["right_pwm"]), (35, 35))
        # A delayed release duplicate must not interrupt a newer valid press.
        await secondary.send_json(release)
        await primary.close()
        for seq in range(2, 8):
            await secondary.send_json(self.command(s, seq))
            s = await self.status(secondary, lambda p: p["seq"] == seq)
            self.assertEqual(s["session_id"], first["session_id"])
            self.assertFalse(s["requires_release"])
            self.assertEqual((s["left_pwm"], s["right_pwm"]), (35, 35))
        self.assertNotIn(first["secret"], json.dumps(await self.health()))
        await secondary.close()
        deadline = time.monotonic() + 1
        while True:
            h = await self.health()
            if h["control"]["reason"] == "controller_disconnected":
                break
            self.assertLess(time.monotonic(), deadline)
            await asyncio.sleep(.01)
        self.assertEqual((h["control"]["left_pwm"], h["control"]["right_pwm"]), (0, 0))
        recovered, attached = await self.attach()
        self.assertNotEqual(first["session_id"], attached["session_id"])
        s = await self.status(recovered)
        self.assertTrue(s["requires_release"])
        await recovered.send_json(self.command(s, 0))
        s = await self.status(recovered, lambda p: p["reason"] == "release_required")
        self.assertEqual((s["left_pwm"], s["right_pwm"]), (0, 0))
        await recovered.close()

    async def test_wrong_attach_secret_does_not_revoke_existing_owner(self):
        ws, owner = await self.attach()
        bad = await self.client.ws_connect(self.origin + "/ws/control/bonded")
        await bad.send_json({"type": "attach", "version": 1, "secret": "0" * 64})
        closed = await asyncio.wait_for(bad.receive(), 2)
        self.assertIn(closed.type, (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED))
        s = await self.status(ws)
        self.assertEqual(s["session_id"], owner["session_id"])
        await bad.close()
        await ws.close()
