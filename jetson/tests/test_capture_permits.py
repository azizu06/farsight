"""Capture provenance and assembled safety checks; no camera or serial is opened."""

import asyncio
import json
import math
import struct
import time
import unittest
from unittest.mock import patch

import cv2
import numpy as np
from starlette.websockets import WebSocketDisconnect

from jetson.camera import CameraFrame, CameraService
from jetson.capture_permits import CapturePermits
from jetson.mapping.phone import Hello
from jetson.mapping.server import create_app
from jetson.mapping.state import MapState
from jetson.mapping.tests.test_mapping import bundle, flat_frame


def rgb_packet(session="test", frame=0, **changes):
    _, image = cv2.imencode(".jpg", np.zeros((6, 8, 3), np.uint8))
    jpeg = image.tobytes()
    header = dict(version=1, type="rgb_frame", session_id=session, map_epoch=1,
                  frame_id=frame, t_capture=100.1, jpeg_len=len(jpeg))
    header.update(changes)
    encoded = json.dumps(header).encode()
    return struct.pack("<I", len(encoded)) + encoded + jpeg


def bound_header(token):
    return dict(session_id="test", map_epoch=1, capture_clock="avcapture_sync_to_host",
                capture_clock_verified=True, capture_bound=True, capture_permit=token,
                capture_permit_received_t=100, t_capture=100.1)


class CapturePermitPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.permits = CapturePermits("test", 1, True, clock=lambda: self.now)
        self.token = self.permits.issue()["token"]
        self.header = bound_header(self.token)

    def test_only_current_connection_tokens_bound_to_qualified_sensor_clock_verify(self):
        self.assertEqual(self.permits.verified_origin(self.header), 10)
        for field, value in (("session_id", "old"), ("map_epoch", 2), ("map_epoch", True),
                             ("capture_permit", "unknown"), ("capture_clock", "arkit_unverified"),
                             ("capture_clock_verified", 1), ("capture_bound", False),
                             ("capture_permit_received_t", 100.1), ("capture_permit_received_t", 101),
                             ("capture_permit_received_t", -1), ("t_capture", math.inf),
                             ("capture_permit_received_t", math.nan), ("t_capture", True)):
            with self.subTest(field=field, value=value):
                self.assertIsNone(self.permits.verified_origin(dict(self.header, **{field: value})))
        reconnect = CapturePermits("test", 1, True, clock=lambda: self.now)
        self.assertIsNone(reconnect.verified_origin(self.header))
        self.permits.close()
        self.assertIsNone(self.permits.verified_origin(self.header))
        self.assertIsNone(self.permits.issue())
        self.assertFalse(self.permits.tokens)

    def test_actual_validation_time_expires_tokens_and_retention_is_bounded(self):
        self.now = 10.249
        self.assertEqual(self.permits.verified_origin(self.header), 10)
        self.now = 10.250
        self.assertIsNone(self.permits.verified_origin(self.header))
        for _ in range(50):
            self.permits.issue()
        self.assertLessEqual(len(self.permits.tokens), 8)
        self.assertNotIn(self.token, self.permits.tokens)
        self.now += 1
        self.permits.issue()
        self.assertEqual(len(self.permits.tokens), 1)

    def test_legacy_hello_remains_supported_and_no_permits_are_issued_by_default(self):
        hello = Hello.model_validate(dict(version=1, type="hello", session_id="test", map_epoch=1,
                                         supports_scene_depth=False))
        self.assertFalse(hello.supports_capture_permits)
        self.assertIsNone(CapturePermits("test", 1).issue())

    def test_camera_origin_is_conservative_and_disconnect_revokes_drive_immediately(self):
        camera = CameraService(MapState())
        self.now = 10.2
        with patch("jetson.camera.time.monotonic", return_value=self.now):
            camera.accept_phone_rgb(rgb_packet(**bound_header(self.token), producer_delay_ms=400),
                                    "test", received_at=10.2, permits=self.permits)
            self.assertAlmostEqual(camera.snapshot().received, 9.8)
            self.assertTrue(camera.status()["camera_drive_ready"])
            preview = camera.preview()
            self.assertTrue(preview.capture_verified)
            self.assertIs(preview.capture_binding, self.permits)
            self.permits.close()
            self.assertTrue(camera.status()["camera"])
            self.assertFalse(camera.status()["camera_drive_ready"])
        # Old/unverified tokens remain viewable and never become drive verified.
        camera.accept_phone_rgb(rgb_packet(frame=1, **bound_header(self.token)), "test", 0,
                                permits=self.permits)
        self.assertTrue(camera.status()["camera"])
        self.assertFalse(camera.status()["camera_drive_ready"])

    def test_receipt_is_never_newer_than_token_issuance_and_expired_frames_stay_viewable(self):
        camera = CameraService(MapState())
        self.now = 10.2
        camera.accept_phone_rgb(rgb_packet(**bound_header(self.token)), "test", received_at=10.2,
                                permits=self.permits)
        self.assertEqual(camera.snapshot().received, 10)
        self.now = 10.3  # includes server decode/queue latency, even if ingress was earlier
        camera.accept_phone_rgb(rgb_packet(frame=1, **bound_header(self.token)), "test", 0,
                                received_at=10.2, permits=self.permits)
        self.assertFalse(camera.snapshot().capture_verified)
        self.assertEqual(camera.snapshot().received, 10.2)

    def test_uvc_simulation_and_replay_freshness_remain_usable(self):
        for device, source in (("/dev/video0", "USB CAMERA"), (None, "SIMULATED"), (None, "REPLAY: PHONE RGB")):
            camera = CameraService(MapState(), device=device)
            frame = CameraFrame(b"jpeg", time.monotonic(), 1, source)
            if device:
                camera.frame = frame
            else:
                camera.phone_rgb = frame
            self.assertTrue(camera.status()["camera_drive_ready"])
        camera = CameraService(MapState())
        camera.phone_rgb = CameraFrame(b"jpeg", time.monotonic(), 1, "IPHONE LIDAR")
        self.assertTrue(camera.status()["camera"])
        self.assertFalse(camera.status()["camera_drive_ready"])


class PhoneSocket:
    def __init__(self, depth=False, permits=True):
        self.hello = dict(version=1, type="hello", session_id="test", map_epoch=1,
                          supports_scene_depth=depth, supports_capture_permits=permits)
        self.incoming, self.outgoing = asyncio.Queue(), asyncio.Queue()
        self.closed = []
    async def accept(self): pass
    async def receive_text(self): return json.dumps(self.hello)
    async def send_json(self, value): await self.outgoing.put(value)
    async def receive_bytes(self):
        packet = await self.incoming.get()
        if packet is None:
            raise WebSocketDisconnect()
        return packet
    async def receive(self):
        packet = await self.incoming.get()
        return dict(type="websocket.disconnect") if packet is None else dict(type="websocket.receive", bytes=packet)
    async def close(self, code=1000, reason=""): self.closed.append((code, reason))


async def until(predicate):
    while not predicate():
        await asyncio.sleep(.001)


def route(app, path):
    return next(item.endpoint for item in app.routes if getattr(item, "path", None) == path)


class CaptureEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_rgb_binding_controls_physical_guard_without_opening_serial(self):
        app = create_app(serial_port="NOT_OPENED", detector="none")  # lifespan/pump deliberately not started
        control, camera = app.state.control.state, app.state.camera
        control.set_connected(True)
        control.update_range(100)
        session = control.begin()
        seq = 0
        def command(deadman=True):
            nonlocal seq
            seq += 1
            status = control.status(issue_permit=True)
            return control.accept(dict(session_id=session, permit=status["permit"], seq=seq,
                                       deadman=deadman, v=.5, w=0, pan=0, mode="teleop",
                                       video_fresh=True, video_age_ms=0), session)
        socket = PhoneSocket()
        task = asyncio.create_task(route(app, "/phone/rgb")(socket))
        try:
            token = (await asyncio.wait_for(socket.outgoing.get(), .2))["token"]
            await socket.incoming.put(rgb_packet())
            await asyncio.wait_for(until(lambda: camera.snapshot() is not None), .2)
            self.assertTrue(camera.status()["camera"])
            self.assertFalse(camera.status()["camera_drive_ready"])
            command(False)
            self.assertFalse(command())
            self.assertEqual(control.output()[:2], (0, 0))
            await socket.incoming.put(rgb_packet(frame=1, **bound_header(token)))
            await asyncio.wait_for(until(lambda: camera.status()["camera_drive_ready"]), .2)
            command(False)
            self.assertTrue(command())
            self.assertEqual(control.output()[:2], (50, 50))
            await socket.incoming.put(None)
            await asyncio.wait_for(task, .2)
            self.assertTrue(camera.status()["camera"])
            self.assertFalse(camera.status()["camera_drive_ready"])
            self.assertEqual(control.output()[:2], (0, 0))
            self.assertTrue(control.requires_release)
            retained = socket.outgoing.qsize()
            await asyncio.sleep(.06)
            self.assertEqual(socket.outgoing.qsize(), retained)
            self.assertIsNone(app.state.phone_owner)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_arkit_legacy_can_map_and_view_but_never_physical_drive(self):
        for capable in (False, True):
            app = create_app(serial_port="NOT_OPENED", detector="none")
            socket = PhoneSocket(depth=True, permits=capable)
            task = asyncio.create_task(route(app, "/phone")(socket))
            try:
                header = {}
                if capable:
                    token = (await asyncio.wait_for(socket.outgoing.get(), .2))["token"]
                    # Even false claims of an AVCapture clock cannot qualify
                    # the ARKit RGB-D path for physical driving.
                    header = bound_header(token)
                await socket.incoming.put(bundle(flat_frame(), **header))
                await asyncio.wait_for(until(lambda: app.state.camera.snapshot() is not None), .3)
                await asyncio.wait_for(until(lambda: app.state.mapping.frames > 0), .3)
                self.assertTrue(app.state.camera.status()["camera"])
                self.assertGreater(app.state.mapping.cloud.snapshot()[0].shape[0], 0)
                self.assertFalse(app.state.camera.status()["camera_drive_ready"])
                self.assertFalse(app.state.control.state.camera_is_ready())
                if not capable:
                    self.assertEqual(socket.outgoing.qsize(), 0)
            finally:
                await socket.incoming.put(None)
                await asyncio.wait_for(task, .3)

    async def test_capture_tokens_pipeline_multiple_expiring_permits_without_backlog(self):
        permits = CapturePermits("test", 1, True)
        messages = []
        class Socket:
            async def send_json(self, message): messages.append((time.monotonic(), message))
        async def receive(): await asyncio.sleep(.17)
        await permits.run(Socket(), receive)
        self.assertGreaterEqual(len(messages), 3)
        self.assertLessEqual(len(messages), 4)
        self.assertEqual(len({message["token"] for _, message in messages}), len(messages))
        for (a, _), (b, message) in zip(messages, messages[1:]):
            self.assertGreaterEqual(b - a, .04)
            self.assertLess(b - a, .1)
            self.assertEqual(message["valid_for_ms"], 250)
        self.assertFalse(permits.active)

    async def test_sender_stall_and_outer_cancellation_close_tokens_and_receiver(self):
        for cancel in (False, True):
            permits = CapturePermits("test", 1, True)
            reached = asyncio.Event()
            class Stalled:
                async def send_json(self, message):
                    reached.set()
                    await asyncio.Event().wait()
            async def receive(): await asyncio.Event().wait()
            task = asyncio.create_task(permits.run(Stalled(), receive))
            await reached.wait()
            if cancel:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            else:
                with self.assertRaises(asyncio.TimeoutError): await asyncio.wait_for(task, .3)
            self.assertFalse(permits.active)
            self.assertFalse(permits.tokens)


if __name__ == "__main__":
    unittest.main()
