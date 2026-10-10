"""Bulk response lifecycles and actual assembled-handler framing/age behavior."""

import asyncio
import gzip
from contextlib import suppress
import math
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, PropertyMock

import cv2
import numpy as np
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect

from jetson.bulk import BulkPacer
from jetson.bulk_response import PacedStreamingResponse, bulk_response
from jetson.camera import CameraFrame
from jetson.mapping.server import create_app
from jetson.video import VideoService
from jetson.tests.test_bulk import AutoClock, ManualClock


SCOPE = dict(type="http", asgi=dict(version="3.0", spec_version="2.4"), method="GET", path="/", headers=[])


def endpoint(app, path):
    routes = [*app.routes, *app.state.control.router.routes]
    return next(route.endpoint for route in routes if getattr(route, "path", None) == path)


async def collect_response(response):
    messages = []
    async def send(message):
        messages.append(message)
    async def receive():
        await asyncio.Event().wait()
    await response(SCOPE, receive, send)
    return dict(messages[0]["headers"]), b"".join(message.get("body", b"") for message in messages[1:]), messages


async def until(predicate):
    while not predicate():
        await asyncio.sleep(0)


class BulkResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_compressed_map_bytes_types_encoding_and_lengths_are_preserved(self):
        app = create_app(detector="none", bulk_limit_mbps=.08)
        clock = AutoClock()
        app.state.bulk._clock, app.state.bulk._sleep = clock, clock.sleep
        state = app.state.mapping
        rng = np.random.default_rng(2)
        state.cloud.integrate(rng.uniform(-2, 2, (500, 3)), rng.integers(0, 255, (500, 3), dtype=np.uint8))
        raw = state.point_packet()
        response = await endpoint(app, "/map/points.bin")(SimpleNamespace(headers={"accept-encoding": "gzip"}))
        self.assertIsInstance(response, PacedStreamingResponse)
        headers, body, messages = await collect_response(response)
        self.assertEqual(headers[b"content-encoding"], b"gzip")
        self.assertEqual(headers[b"vary"], b"Accept-Encoding")
        self.assertEqual(headers[b"content-type"], b"application/octet-stream")
        self.assertEqual(int(headers[b"content-length"]), len(body))
        self.assertEqual(gzip.decompress(body), raw)
        chunks = [message["body"] for message in messages[1:] if message.get("body")]
        self.assertGreater(len(chunks), 1)
        self.assertLessEqual(max(map(len, chunks)), 250)
        self.assertGreaterEqual(clock.now, (len(body) - 250) / 10000 - 1e-9)
        self.assertEqual(app.state.bulk.pending_requests, 0)

    async def test_jpeg_depth_tts_mesh_ply_and_default_mode_keep_payloads_and_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            for rate in (0, .08):
                app = create_app(detector="none", tts_dir=directory, bulk_limit_mbps=rate)
                clock = AutoClock()
                app.state.bulk._clock, app.state.bulk._sleep = clock, clock.sleep
                image = np.zeros((12, 16, 3), np.uint8)
                _, jpg = cv2.imencode(".jpg", image)
                state = app.state.mapping
                state.jpeg, state.depth_jpeg = jpg.tobytes(), jpg.tobytes()
                state.last_frame_received, state.frame_id = time.monotonic(), 7
                app.state.camera.phone_rgb = CameraFrame(jpg.tobytes(), time.monotonic(), 7, "IPHONE LIDAR")
                voice = Path(directory) / "reply-test.wav"
                voice.write_bytes(b"RIFF test bytes")
                response = await endpoint(app, "/camera.jpg")()
                if rate == 0:
                    self.assertIs(type(response), Response)
                headers, body, _ = await collect_response(response)
                self.assertTrue(body.startswith(b"\xff\xd8"))
                self.assertEqual(headers[b"x-frame-id"], b"7")
                self.assertEqual(headers[b"x-frame-source"], b"IPHONE LIDAR")
                self.assertIn(b"x-frame-age-ms", headers)
                self.assertEqual(headers[b"cache-control"], b"no-store")
                self.assertEqual(int(headers[b"content-length"]), len(body))
                for path, args, expected, content_type in (
                        ("/depth.jpg", (), state.depth_jpeg, b"image/jpeg"),
                        ("/tts/{name}", (voice.name,), voice.read_bytes(), b"audio/wav"),
                        ("/map/mesh.bin", (SimpleNamespace(headers={}),), state.mesh_packet(), b"application/octet-stream"),
                        ("/map/export.ply", (SimpleNamespace(headers={}),), state.ply(), b"application/octet-stream")):
                    response = await endpoint(app, path)(*args)
                    headers, body, _ = await collect_response(response)
                    self.assertEqual(body, expected)
                    self.assertEqual(headers[b"content-type"], content_type)
                    self.assertEqual(int(headers[b"content-length"]), len(body))
                    if path.endswith(".ply"):
                        self.assertIn(b"content-disposition", headers)

    async def test_disconnect_during_delayed_send_closes_iterator_before_spending_more(self):
        clock = ManualClock()
        pacer = BulkPacer(.08, clock=clock, sleep=clock.sleep)
        disconnected, sending = asyncio.Event(), asyncio.Event()
        calls = []
        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                calls.append(message["body"])
                sending.set()
                await asyncio.Event().wait()
        async def receive():
            await disconnected.wait()
            return dict(type="http.disconnect")
        response = bulk_response(b"x" * 2000, pacer, media_type="application/octet-stream")
        task = asyncio.create_task(response(SCOPE, receive, send))
        await sending.wait()
        self.assertEqual(pacer.pending_requests, 1)
        disconnected.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(pacer.pending_requests, 0)
        received = []
        async def other(data):
            received.append(data)
        successor = asyncio.create_task(pacer.send_packet(b"n" * 100, other))
        await asyncio.sleep(0)
        clock.advance(.011)
        await successor
        self.assertEqual(received, [b"n" * 100])

    async def test_response_cancellation_and_send_error_release_held_budget(self):
        pacer = BulkPacer(.08)
        reached = asyncio.Event()
        async def send(message):
            if message.get("body"):
                reached.set()
                await asyncio.Event().wait()
        async def receive():
            await asyncio.Event().wait()
        response = bulk_response(b"x" * 1000, pacer, media_type="application/octet-stream")
        task = asyncio.create_task(response(SCOPE, receive, send))
        await reached.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(pacer.pending_requests, 0)
        async def failed(message):
            if message.get("body"):
                raise OSError("disconnected transport")
        await bulk_response(b"y" * 1000, pacer, media_type="application/octet-stream")(SCOPE, receive, failed)
        self.assertEqual(pacer.pending_requests, 0)

    async def test_bulk_stall_does_not_block_actual_control_status_or_health_handlers(self):
        app = create_app(detector="none", bulk_limit_mbps=.08)
        stalled = asyncio.Event()
        async def send(message):
            if message.get("body"):
                stalled.set()
                await asyncio.Event().wait()
        async def receive():
            await asyncio.Event().wait()
        task = asyncio.create_task(bulk_response(b"b" * 1000, app.state.bulk, media_type="application/octet-stream")(SCOPE, receive, send))
        await stalled.wait()
        control = await asyncio.wait_for(endpoint(app, "/control/status")(), .2)
        health = await asyncio.wait_for(endpoint(app, "/health")(), .2)
        self.assertFalse(control["rover"])
        self.assertFalse(health["rover"])
        socket = ControlSocket()
        controller = asyncio.create_task(endpoint(app, "/ws/control")(socket))
        await asyncio.wait_for(socket.status_received.wait(), .2)
        self.assertTrue(socket.statuses[0]["permit"])
        self.assertTrue(socket.statuses[0]["session_id"])
        socket.disconnect.set()
        await asyncio.wait_for(controller, .2)
        self.assertEqual(app.state.bulk.pending_requests, 1)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.assertEqual(app.state.bulk.pending_requests, 0)

    async def test_bulk_cap_paces_map_but_never_live_camera(self):
        app = create_app(detector="none", bulk_limit_mbps=.08)
        state = app.state.mapping
        state.cloud.integrate(np.random.default_rng(3).uniform(-2, 2, (500, 3)), np.full((500, 3), 9))
        _, encoded = cv2.imencode(".jpg", np.zeros((12, 16, 3), np.uint8))
        app.state.camera.phone_rgb = CameraFrame(encoded.tobytes(), time.monotonic(), 3, "PHONE RGB")
        state.depth_jpeg = encoded.tobytes()
        self.assertIsInstance(await endpoint(app, "/map/points.bin")(SimpleNamespace(headers={})), PacedStreamingResponse)
        self.assertIs(type(await endpoint(app, "/camera.jpg")()), Response)
        self.assertIs(type(await endpoint(app, "/depth.jpg")()), Response)
        self.assertFalse(app.state.live.enabled)

    async def test_mjpeg_framing_survives_chunking_and_disconnect_closes_source(self):
        app = create_app(detector="none", video_limit_mbps=.08)
        clock = AutoClock()
        app.state.live._clock, app.state.live._sleep = clock, clock.sleep
        _, encoded = cv2.imencode(".jpg", np.zeros((12, 16, 3), np.uint8))
        app.state.camera.phone_rgb = CameraFrame(encoded.tobytes(), time.monotonic(), 1, "PHONE RGB")
        expected = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + app.state.camera.preview().jpeg + b"\r\n"
        response = await endpoint(app, "/stream.mjpg")()
        messages, disconnected = [], asyncio.Event()
        async def send(message):
            messages.append(message)
            if sum(len(item.get("body", b"")) for item in messages) >= len(expected):
                disconnected.set()
        async def receive():
            await disconnected.wait()
            return dict(type="http.disconnect")
        await asyncio.wait_for(response(SCOPE, receive, send), 1)
        self.assertEqual(b"".join(item.get("body", b"") for item in messages), expected)
        self.assertIn(b"boundary=frame", dict(messages[0]["headers"])[b"content-type"])
        self.assertGreater(len(messages), 3)
        self.assertEqual(app.state.live.pending_requests, 0)
        self.assertIsNone(response.source.ag_frame)

    def test_invalid_bulk_rate_is_rejected_before_startup(self):
        for rate in (-1, math.nan, math.inf, True):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                create_app(bulk_limit_mbps=rate)


class ControlSocket:
    def __init__(self):
        self.statuses = []
        self.status_received, self.disconnect = asyncio.Event(), asyncio.Event()
    async def accept(self): pass
    async def send_json(self, status):
        self.statuses.append(status)
        self.status_received.set()
    async def receive(self):
        await self.disconnect.wait()
        return dict(type="websocket.disconnect")
    async def close(self, **options): pass


class FakeSocket:
    def __init__(self):
        self.events, self.closed = [], []
        self.sent_segment = asyncio.Event()
        self.requested = False
        self.query_params = {}
    async def accept(self): pass
    async def send_json(self, value):
        self.events.append(("json", value, time.monotonic()))
    async def send_bytes(self, value):
        self.events.append(("bytes", value, time.monotonic()))
        if len(self.events) >= 4:
            self.sent_segment.set()
    async def receive_json(self):
        if not self.requested:
            self.requested = True
            return dict(type="next", request_id=12)
        await asyncio.Event().wait()
    async def close(self, code=1000, reason=""):
        self.closed.append((code, reason))
    async def receive(self):
        await asyncio.Event().wait()


class BulkVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_disconnect_during_initial_admission_releases_shared_budget(self):
        app = create_app(detector="none", h264=True, video_limit_mbps=.08)
        clock = ManualClock()
        app.state.live._clock, app.state.live._sleep = clock, clock.sleep
        app.state.video.init, app.state.video.mime = b"x" * 1000, "video/mp4"
        ws = FakeSocket()
        disconnected = asyncio.Event()
        async def receive_json():
            await disconnected.wait()
            raise WebSocketDisconnect()
        ws.receive_json = receive_json
        with patch.object(VideoService, "ready", new_callable=PropertyMock, return_value=True):
            task = asyncio.create_task(endpoint(app, "/ws/video")(ws))
            await asyncio.wait_for(until(lambda: app.state.live.pending_requests > 0), 1)
            disconnected.set()
            await asyncio.wait_for(task, 1)
        self.assertEqual(app.state.live.pending_requests, 0)
        self.assertFalse(any(kind == "bytes" for kind, _, _ in ws.events))
        self.assertFalse(app.state.video.subscribers)

    async def test_video_packet_boundaries_age_after_admission_and_readiness_wait_are_preserved(self):
        app = create_app(detector="none", h264=True, video_limit_mbps=.08)
        video = app.state.video
        video.init, video.mime = b"init opaque bytes", "video/mp4; codecs=fixture"
        ws = FakeSocket()
        with patch.object(VideoService, "ready", new_callable=PropertyMock, return_value=True):
            task = asyncio.create_task(endpoint(app, "/ws/video")(ws))
            while not video.subscribers:
                await asyncio.sleep(0)
            captured = time.monotonic()
            payload = b"opaque whole MP4 fragment" * 40
            video.publish(dict(payload=payload, received=captured, frame=9, source="USB CAMERA",
                               media_start_s=0, media_end_s=.1, samples=[dict(frame=9)]))
            await asyncio.wait_for(ws.sent_segment.wait(), 1)
            metadata = next(value for kind, value, _ in ws.events if kind == "json" and value["type"] == "video_segment")
            binary = [value for kind, value, _ in ws.events if kind == "bytes"]
            self.assertEqual(binary, [video.init, payload])
            self.assertEqual(metadata["request_id"], 12)
            self.assertGreaterEqual(metadata["age_ms"], 60)
            self.assertLess(metadata["server_wait_ms"], 30)  # admission was not readiness wait
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self.assertEqual(app.state.live.pending_requests, 0)

    async def test_oversized_video_packet_closes_with_useful_reason(self):
        app = create_app(detector="none", h264=True, video_limit_mbps=.08)
        app.state.video.init, app.state.video.mime = b"x" * 262145, "video/mp4"
        ws = FakeSocket()
        with patch.object(VideoService, "ready", new_callable=PropertyMock, return_value=True):
            await endpoint(app, "/ws/video")(ws)
        self.assertEqual(ws.closed[0][0], 1009)
        self.assertIn("bitrate", ws.closed[0][1])
        self.assertFalse(any(kind == "bytes" for kind, _, _ in ws.events))

    async def test_large_legacy_map_closes_cleanly_but_http_status_stays_unpaced(self):
        app = create_app(detector="none", bulk_limit_mbps=.000008)
        socket = FakeSocket()
        with patch.object(app.state.mapping, "point_packet", return_value=b"x" * 262145):
            await asyncio.wait_for(endpoint(app, "/ws/map")(socket), 1)
        self.assertEqual(socket.closed[0][0], 1009)
        self.assertIn("points=http", socket.closed[0][1])
        self.assertFalse(any(kind == "bytes" for kind, _, _ in socket.events))
        socket = FakeSocket()
        socket.query_params = dict(points="http")
        task = asyncio.create_task(endpoint(app, "/ws/map")(socket))
        try:
            await asyncio.wait_for(until(lambda: socket.events), .2)
            self.assertEqual(socket.events[0][1]["points_transport"], "http")
            self.assertEqual(app.state.bulk.pending_requests, 0)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


if __name__ == "__main__":
    unittest.main()
