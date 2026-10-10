"""Reproducible simulated capture/control/FFmpeg/relay qualification.

python -m tools.tests.test_demo_qualification --report /tmp/farsight-qualification.json
python -m unittest tools.tests.test_demo_qualification -v

No hardware is opened. RGB frames are generated locally; motors are simulated.
Arrival age is measured, while Quest decode/display and physical stop are not.
"""

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import json
import math
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

from tools.fault_proxy import FaultProxy
from tools.operator_relay import create_app as create_relay, upstream_origin
from tools.qualify_link import capture_receipt_bound, measure, percentile

ROOT = Path(__file__).resolve().parents[2]


def fixture_app(source):
    """The production services with an explicitly synthetic RGB producer."""
    import cv2
    import numpy as np
    from fastapi import HTTPException, Request
    from jetson.camera import CameraFrame
    from jetson.mapping.server import create_app

    app = create_app(demo=source == "map5", simulate_rover=True, detector="none",
                     h264=True, video_fps=10, video_bitrate=500000)
    original_lifespan = app.router.lifespan_context
    stop_events = []
    original_stop = app.state.control.state.stop
    original_accept = app.state.control.state.accept
    def record_stop(reason, *args, **kwargs):
        if reason != "deadman_released":
            stop_events.append(dict(reason=reason, at=time.monotonic(), seq=app.state.control.state.seq))
            del stop_events[:-100]
        return original_stop(reason, *args, **kwargs)
    def record_accept(command, session_id):
        state = app.state.control.state
        with state.lock:
            expiry = state.permits.get(command.get("permit"), 0) if isinstance(command, dict) else 0
            now = state.clock()
            active = isinstance(command, dict) and command.get("deadman") is True
            result = original_accept(command, session_id)
            if active and not result:
                stop_events.append(dict(reason="command_rejected:" + state.reason, at=now,
                    seq=command.get("seq"), permit_age_ms=(state.LEASE - (expiry - now)) * 1000))
                del stop_events[:-100]
            return result
    app.state.control.state.stop = record_stop
    app.state.control.state.accept = record_accept

    async def feed_rgb():
        # A moving textured synthetic scene stresses the real encoder more than
        # a repeated black image. Timestamps include JPEG generation latency.
        rng = np.random.default_rng(20261010)
        base = rng.integers(0, 256, (120, 160, 3), np.uint8).repeat(4, 0).repeat(4, 1)
        frame_id = 0
        while True:
            captured = time.monotonic()
            image = np.roll(base, frame_id * 4, axis=1)
            x = frame_id * 9 % 540
            image[160:240, x:x + 100] = (230, 30, 90)
            ok, jpeg = await asyncio.to_thread(cv2.imencode, ".jpg", image,
                                               [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not ok:
                raise RuntimeError("Synthetic camera JPEG encoding failed")
            app.state.camera.phone_rgb = CameraFrame(jpeg.tobytes(), captured, frame_id,
                                                     "SIMULATED RGB 15 FPS")
            frame_id += 1
            await asyncio.sleep(max(0, 1 / 15 - (time.monotonic() - captured)))

    @asynccontextmanager
    async def lifespan(app):
        async with original_lifespan(app):
            producer = asyncio.create_task(feed_rgb()) if source == "rgb15" else None
            try:
                yield
            finally:
                if producer:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)

    app.router.lifespan_context = lifespan

    def fixture_only(request):
        if (request.client.host not in ("127.0.0.1", "::1")
                or not app.state.control.state.simulated):
            raise HTTPException(403, "Test fixture requires localhost and simulated motors")

    @app.get("/__qualification/status")
    async def fixture_status(request: Request):
        fixture_only(request)
        process = app.state.video.process
        return dict(simulated=True, source=source, encoder_pid=process.pid if process else None,
                    encoder_alive=bool(process and process.returncode is None),
                    video_ready=app.state.video.ready, stop_events=stop_events.copy())

    @app.post("/__qualification/kill-encoder")
    async def kill_encoder(request: Request):
        fixture_only(request)
        process = app.state.video.process
        if process is None or process.returncode is not None:
            raise HTTPException(409, "Encoder is not running")
        pid = process.pid
        process.kill()
        return dict(killed_pid=pid, simulated=True)

    # The production app mounts its static viewer at /. Fixture-only routes
    # must precede that catch-all mount to remain reachable.
    app.router.routes.sort(key=lambda route: 0 if getattr(route, "path", "").startswith("/__qualification/") else 1)

    return app


class Rig:
    def __init__(self, source="rgb15", delay_ms=50, bandwidth_mbps=2):
        self.source, self.delay_ms, self.bandwidth_mbps = source, delay_ms, bandwidth_mbps
        self.process = self.proxy = self.relay = self.client = None
        self.temp = tempfile.TemporaryDirectory()
        self.log = open(Path(self.temp.name) / "fixture.log", "w+")

    async def __aenter__(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
        self.direct = f"http://127.0.0.1:{port}"
        self.process = subprocess.Popen([sys.executable, "-m", "tools.tests.test_demo_qualification",
            "--serve-fixture", self.source, "--port", str(port)], cwd=ROOT,
            stdout=self.log, stderr=self.log, start_new_session=True)
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5))
        try:
            for _ in range(100):
                if self.process.poll() is not None:
                    self.log.seek(0)
                    raise RuntimeError("Fixture exited: " + self.log.read()[-3000:])
                try:
                    async with self.client.get(self.direct + "/health") as response:
                        health = await response.json()
                        if health["camera"] and health["video"]["ready"]:
                            if not health["control"]["simulated"]:
                                raise RuntimeError("Qualification must never use physical motors")
                            break
                except (aiohttp.ClientError, ValueError):
                    pass
                await asyncio.sleep(.05)
            else:
                raise RuntimeError("Fixture did not become ready")
            self.proxy = await FaultProxy(port, port=0, delay_ms=self.delay_ms,
                                          bandwidth_mbps=self.bandwidth_mbps).start()
            from aiohttp import web
            self.relay = web.AppRunner(create_relay(f"http://127.0.0.1:{self.proxy.port}"), shutdown_timeout=.25)
            await self.relay.setup()
            site = web.TCPSite(self.relay, "127.0.0.1", 0)
            await site.start()
            self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def status(self):
        async with self.client.get(self.direct + "/control/status") as response:
            return await response.json()

    async def fixture_status(self):
        async with self.client.get(self.direct + "/__qualification/status") as response:
            return await response.json()

    async def kill_encoder(self):
        async with self.client.post(self.direct + "/__qualification/kill-encoder") as response:
            response.raise_for_status()
            return await response.json()

    async def __aexit__(self, *args):
        if self.client:
            await self.client.close()
        if self.relay:
            await self.relay.cleanup()
        if self.proxy:
            await self.proxy.close()
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(self.process.wait, 5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                await asyncio.to_thread(self.process.wait)
        self.log.close()
        self.temp.cleanup()


class VideoClient:
    """Two complete fragments in flight; reports arrival, not decoded display."""
    def __init__(self, url, window=2):
        self.url = url
        self.window = window
        self.at, self.age = -float("inf"), float("inf")
        self.connected = False
        self.bytes = 0
        self.bounds, self.cadences, self.errors, self.inits = [], [], [], []
        self.durations, self.sample_counts = [], []
        self.payload_sizes = []
        self.task = self.client = None
        self.ws = None
        self.stopping = False
        self.freeze = False

    def current_age(self):
        return self.age + (time.monotonic() - self.at) * 1000 if self.connected else float("inf")

    async def start(self):
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=3))
        self.task = asyncio.create_task(self.consume())

    async def consume(self):
        try:
            async with self.client.ws_connect(self.url + "/ws/video",
                    timeout=aiohttp.ClientWSTimeout(ws_close=.25)) as ws:
                self.ws = ws
                initialization = await asyncio.wait_for(ws.receive_json(), 3)
                if initialization.get("type") != "video_init":
                    raise ValueError(f"Video initialization missing: {initialization}")
                data = await asyncio.wait_for(ws.receive_bytes(), 2)
                if b"ftyp" not in data or b"moov" not in data:
                    raise ValueError("Actual MP4 initialization atoms missing")
                self.inits.append(dict(metadata=initialization, bytes=len(data), at=time.monotonic()))
                self.bytes += len(data)
                requested, request_id = {}, 0
                async def request():
                    nonlocal request_id
                    request_id += 1
                    requested[request_id] = time.monotonic()
                    await ws.send_json(dict(type="next", request_id=request_id))
                for _ in range(self.window):
                    await request()
                previous = None
                while not self.stopping:
                    if self.freeze:
                        await asyncio.sleep(.01)
                        continue
                    metadata = await asyncio.wait_for(ws.receive_json(), 2)
                    data = await asyncio.wait_for(ws.receive_bytes(), 2)
                    received = time.monotonic()
                    identifier = metadata.get("request_id")
                    if type(identifier) is not int or identifier not in requested or b"moof" not in data:
                        raise ValueError("Actual fragment or matching metadata missing")
                    elapsed = (received - requested.pop(identifier)) * 1000
                    self.age = capture_receipt_bound(metadata.get("age_ms"), elapsed, metadata.get("server_wait_ms"))
                    self.at, self.connected = received, True
                    self.bounds.append(self.age)
                    self.durations.append((metadata["media_end_s"] - metadata["media_start_s"]) * 1000)
                    self.sample_counts.append(len(metadata.get("samples", [])))
                    self.payload_sizes.append(len(data))
                    self.bytes += len(data)
                    if previous is not None:
                        self.cadences.append((received - previous) * 1000)
                    previous = received
                    await request()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            if not self.stopping:
                self.errors.append(type(exc).__name__)
        finally:
            self.connected = False

    async def close(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
        if self.ws:
            with suppress(asyncio.TimeoutError, aiohttp.ClientError):
                await asyncio.wait_for(self.ws.close(), .3)
        if self.client:
            await self.client.close()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)


class Controller:
    def __init__(self, rig, video):
        self.rig, self.video = rig, video
        self.status = {}
        self.armed = False
        self.held = False
        self.seq = 0
        self.sent, self.acks, self.cadences = {}, [], []
        self.reasons = set()
        self.invalidations = []
        self.tasks = []
        self.client = self.ws = None
        self.stopping = False
        self.errors = []

    async def start(self):
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=3))
        self.ws = await self.client.ws_connect(self.rig.url + "/ws/control",
                                               timeout=aiohttp.ClientWSTimeout(ws_close=.25))
        self.status = await asyncio.wait_for(self.ws.receive_json(), 3)
        if not self.status.get("simulated"):
            raise RuntimeError("Controller fixture refuses physical motors")
        self.tasks = [asyncio.create_task(self.receive()), asyncio.create_task(self.send())]

    async def receive(self):
        try:
            while not self.stopping:
                status = await self.ws.receive_json()
                self.status = status
                seq = status.get("seq")
                self.reasons.add(status.get("reason"))
                if seq in self.sent:
                    self.acks.append((time.monotonic() - self.sent.pop(seq)) * 1000)
                if status.get("requires_release") and self.held:
                    if self.armed:
                        self.invalidations.append(dict(kind="server_release_required", reason=status.get("reason"),
                                                       video_age_ms=self.video.current_age()
                                                       if math.isfinite(self.video.current_age()) else None,
                                                       seq=status.get("seq")))
                    self.armed = False
        except (aiohttp.ClientError, TypeError, ValueError) as exc:
            if not self.stopping:
                self.errors.append(type(exc).__name__)
            self.armed = False

    async def send(self):
        previous = None
        try:
            while not self.stopping:
                now = time.monotonic()
                age = self.video.current_age()
                fresh = age <= 500
                if not fresh:
                    if self.armed:
                        self.invalidations.append(dict(kind="view_stale", video_age_ms=age,
                                                       arrival_age_ms=self.video.age))
                        if not math.isfinite(age):
                            self.invalidations[-1]["video_age_ms"] = None
                    self.armed = False
                active = bool(self.held and self.armed and fresh)
                command = dict(session_id=self.status["session_id"], seq=self.seq, permit=self.status.get("permit"),
                    deadman=active, v=.5 if active else 0, w=0, pan=0, mode="teleop", video_fresh=fresh,
                    video_age_ms=round(age) if fresh else 999999)
                self.sent[self.seq] = now
                self.seq += 1
                if len(self.sent) > 100:
                    del self.sent[min(self.sent)]
                await self.ws.send_json(command)
                if previous is not None:
                    self.cadences.append((now - previous) * 1000)
                previous = now
                await asyncio.sleep(max(0, .05 - (time.monotonic() - now)))
        except (aiohttp.ClientError, KeyError) as exc:
            if not self.stopping:
                self.errors.append(type(exc).__name__)

    async def release(self):
        self.held = self.armed = False
        await asyncio.sleep(.3)

    def press(self):
        if self.video.current_age() > 500 or self.status.get("requires_release"):
            return False
        self.held = self.armed = True
        return True

    async def close(self):
        self.stopping = True
        self.held = self.armed = False
        for task in self.tasks:
            task.cancel()
        if self.ws:
            with suppress(asyncio.TimeoutError, aiohttp.ClientError):
                await asyncio.wait_for(self.ws.close(), .3)
        if self.client:
            await self.client.close()
        await asyncio.gather(*self.tasks, return_exceptions=True)


async def wait_until(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = predicate()
        if result:
            return result
        await asyncio.sleep(.02)
    raise RuntimeError("Qualification condition timed out")


async def motion_trial(rig, duration=8, fault=None, window=2):
    video = VideoClient(rig.url, window=window)
    controller = Controller(rig, video)
    samples, stopped_ms, before_pid, after_pid = [], None, None, None
    started = fault_at = None
    try:
        await video.start()
        await controller.start()
        await wait_until(lambda: len(video.bounds) >= 10 and video.current_age() <= 400)
        await controller.release()
        await wait_until(lambda: not controller.status.get("requires_release") and video.current_age() <= 400)
        if not controller.press():
            raise RuntimeError("Fresh view and release required before simulated motion")
        started = time.monotonic()
        until = started + duration
        while time.monotonic() < until:
            elapsed = time.monotonic() - started
            if fault and fault_at is None and elapsed >= 2:
                before_fault = await rig.status()
                if not (before_fault["left_pwm"] or before_fault["right_pwm"]):
                    raise RuntimeError("Motion had already stopped before fault injection; stop timing is unqualified")
                fault_at = time.monotonic()
                if fault == "encoder":
                    before_pid = (await rig.kill_encoder())["killed_pid"]
                elif fault == "freeze":
                    video.freeze = True
                elif fault == "outage":
                    rig.proxy.drop()
                else:
                    raise ValueError("Unknown trial fault")
            status = await rig.status()
            moving = bool(status["left_pwm"] or status["right_pwm"])
            samples.append((elapsed, moving, status["reason"], video.current_age()))
            if fault_at and stopped_ms is None and not moving:
                stopped_ms = (time.monotonic() - fault_at) * 1000
            await asyncio.sleep(.04)
        result = dict(source=rig.source, seconds=duration, request_window=window, samples=len(samples),
            moving_samples=sum(value[1] for value in samples),
            stable_moving_fraction=sum(value[1] for value in samples if value[0] >= 1)
                / max(1, sum(value[0] >= 1 for value in samples)),
            capture_receipt_p95_ms=percentile(video.bounds, .95),
            capture_receipt_max_ms=max(video.bounds) if video.bounds else None,
            held_view_age_max_ms=max((value[3] for value in samples if math.isfinite(value[3])), default=None),
            held_stale_samples=sum(not math.isfinite(value[3]) or value[3] > 500 for value in samples),
            fragment_cadence_p95_ms=percentile(video.cadences, .95),
            fragment_media_duration_p95_ms=percentile(video.durations, .95),
            fragment_sample_counts=sorted(set(video.sample_counts)),
            fragment_payload_p95_bytes=percentile(video.payload_sizes, .95),
            fragment_payload_max_bytes=max(video.payload_sizes) if video.payload_sizes else None,
            control_ack_p95_ms=percentile(controller.acks, .95),
            command_cadence_p95_ms=percentile(controller.cadences, .95),
            reasons=sorted(value for value in controller.reasons if value),
            invalidations=controller.invalidations.copy(),
            video_errors=video.errors.copy(), control_errors=controller.errors.copy(),
            fault=fault, fault_to_observed_simulated_stop_ms=stopped_ms)
        result["backend_stop_events"] = (await rig.fixture_status())["stop_events"]
        if fault == "encoder":
            old_video = video
            await old_video.close()
            recovered = None
            for _ in range(100):
                recovered = await rig.fixture_status()
                if recovered["video_ready"] and recovered["encoder_pid"] != before_pid:
                    break
                await asyncio.sleep(.05)
            else:
                raise RuntimeError("Encoder supervisor did not recover")
            after_pid = recovered["encoder_pid"]
            video = VideoClient(rig.url, window=window)
            controller.video = video
            await video.start()
            await wait_until(lambda: len(video.bounds) >= 5 and video.current_age() <= 400)
            await asyncio.sleep(.35)
            stopped_without_press = await rig.status()
            result.update(encoder_pid_changed=before_pid != after_pid,
                          recovered_mp4_init=bool(video.inits),
                          stopped_before_repress=not (stopped_without_press["left_pwm"] or stopped_without_press["right_pwm"]))
            await controller.release()
            result["explicit_repress_accepted"] = controller.press()
            await asyncio.sleep(.3)
            rearmed = await rig.status()
            result["moves_after_explicit_repress"] = bool(rearmed["left_pwm"] or rearmed["right_pwm"])
        return result
    finally:
        await controller.close()
        await video.close()
        await asyncio.sleep(.3)


async def run_report(output=None):
    results = dict(hardware_verified=False, host=platform.platform(), python=platform.python_version(),
                   condition=dict(delay_each_direction_ms=50, aggregate_mbps=2,
                                  encoder_bitrate=500000, encoder_fps=10, request_window=2),
                   not_verified=["Quest decoding/display", "physical sensor exposure delay", "Jetson performance",
                                 "cellular radio", "USB tethering", "physical Uno watchdog"])
    def record(key, value):
        results[key] = value
        print(f"Recorded {key}", file=sys.stderr, flush=True)
        if output:
            output.write_text(json.dumps(results, indent=2) + "\n")
    for source in ("rgb15", "map5"):
        async with Rig(source) as rig:
            record(source + "_stop_only_qualifier", await measure(upstream_origin(rig.url), seconds=10,
                                                                         video_mode="h264"))
            await asyncio.sleep(.3)
            try:
                record(source + "_motion", await motion_trial(rig))
            except RuntimeError as exc:
                record(source + "_motion", dict(error=str(exc)))
    for fault in ("encoder", "freeze", "outage"):
        async with Rig("rgb15") as rig:
            try:
                record(fault, await motion_trial(rig, duration=5, fault=fault))
            except RuntimeError as exc:
                record(fault, dict(error=str(exc)))
    return results


@unittest.skipUnless(shutil.which("ffmpeg"), "Actual H264 qualification requires ffmpeg")
class QualificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # IsolatedAsyncioTestCase enables expensive per-task traceback recording.
        # These tests measure real timing and must use the production loop mode.
        asyncio.get_running_loop().set_debug(False)

    @unittest.skipUnless(os.environ.get("FARSIGHT_TIMING_GATES") == "1",
                         "Sustained timing is an opt-in benchmark on a quiet host")
    async def test_rgb_capture_and_fixed_permits_sustain_simulated_motion(self):
        async with Rig("rgb15") as rig:
            result = await motion_trial(rig, duration=5)
            self.assertGreaterEqual(result["stable_moving_fraction"], .95, result)
            self.assertEqual(result["held_stale_samples"], 0, result)
            self.assertFalse(result["invalidations"], result)

    async def test_fault_proxy_disables_nagle_on_both_sockets(self):
        async with Rig("rgb15") as rig:
            async with rig.client.get(rig.url + "/health") as response:
                self.assertEqual(response.status, 200)
                await response.read()
            self.assertEqual(len(rig.proxy.writers), 2)
            for writer in rig.proxy.writers:
                # Darwin returns the internal flag bit (4), Linux returns 1;
                # either is enabled. The configured setsockopt value is 1.
                self.assertTrue(writer.get_extra_info("socket").getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY))

    async def test_actual_encoder_restart_resends_init_and_requires_repress(self):
        async with Rig("rgb15") as rig:
            result = await motion_trial(rig, duration=4, fault="encoder")
            self.assertTrue(result["encoder_pid_changed"], result)
            self.assertTrue(result["recovered_mp4_init"], result)
            self.assertTrue(result["stopped_before_repress"], result)
            self.assertTrue(result["moves_after_explicit_repress"], result)
            self.assertLess(result["fault_to_observed_simulated_stop_ms"], 500, result)

    async def test_video_freeze_and_wire_outage_stop_simulated_motion(self):
        for fault in ("freeze", "outage"):
            async with Rig("rgb15") as rig:
                result = await motion_trial(rig, duration=4, fault=fault)
                self.assertIsNotNone(result["fault_to_observed_simulated_stop_ms"], result)
                self.assertLess(result["fault_to_observed_simulated_stop_ms"], 750, result)
                stopped = await rig.status()
                self.assertEqual((stopped["left_pwm"], stopped["right_pwm"]), (0, 0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-fixture", choices=("rgb15", "map5"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.serve_fixture:
        if not args.port:
            parser.error("Fixture requires an explicit ephemeral localhost port")
        import uvicorn
        uvicorn.run(fixture_app(args.serve_fixture), host="127.0.0.1", port=args.port,
                    access_log=False, log_level="warning", timeout_graceful_shutdown=2)
    else:
        result = asyncio.run(run_report(args.report))
        text = json.dumps(result, indent=2)
        print(text)
        if args.report:
            args.report.write_text(text + "\n")


if __name__ == "__main__":
    main()
