"""Optional read-only same-frame RGB-D export for the desktop reconstruction lab.

Service owner mounts with mount(app) after create_app. This is NOT enabled in the
rig unless FARSIGHT_RAW_CAPTURE=1; installing this module alone is inactive.
"""
import asyncio
import json
import struct
import threading
import time

from fastapi import HTTPException, Request
from starlette.responses import Response
from jetson.camera import adjusted_receipt
from jetson.bulk_response import bulk_response
from .compress import EncodedCache


class RawCapture:
    def __init__(self, state):
        self.state = state
        self.latest = None
        self.lock = threading.Lock()
        self.cached_key = None
        self.cached_body = None
        self.encoded = EncodedCache()

    def observe(self, frame):
        # Called after accepted-frame validation. Retain ONE immutable decoded
        # observation by reference; no encoding/copy/fusion on the ingest path.
        with self.state.lock:
            if (frame.header.get('_mapping_socket_generation') != self.state.socket_generation
                    or not self.state.connected):
                return
            self.latest = (frame, self.state.generation)

    def snapshot(self, after=None):
        with self.state.lock:
            value = self.latest
            if value is None:
                raise HTTPException(503, 'Waiting for accepted RGB-D')
            frame, generation = value
            h = frame.header
            if (generation != self.state.generation or not self.state.connected
                    or (h['session_id'], h['map_epoch']) != self.state.identity
                    or h.get('_mapping_socket_generation') != self.state.socket_generation
                    or self.state.paused or h['tracking'] != 'normal' or self.state.tracking != 'normal'):
                raise HTTPException(503, 'RGB-D capture scope unavailable')
            age = time.monotonic() - adjusted_receipt(h, h.get('_received_monotonic', 0))
            if age < 0 or age > 2:
                raise HTTPException(503, 'RGB-D capture stale')
            key = f"{h['session_id']}:{h['map_epoch']}:{generation}:{h['frame_id']}"
        headers = {'Cache-Control': 'no-store', 'X-Capture-Key': key,
                   'X-Capture-Age-Ms': str(round(age * 1000, 2))}
        if after == key:
            return None, headers
        with self.lock:
            if key != self.cached_key:
                header = {k: v for k, v in h.items() if not k.startswith('_')}
                header['map_generation'] = generation
                header['capture_source'] = 'SIMULATED' if self.state.demo else 'IPHONE LIDAR'
                # Preserve native JPEG/intrinsics, depth/confidence and ARKit pose
                # from one capture; do not substitute the rotated live camera JPEG.
                header['image'] = {**header['image'], 'jpeg_len': len(frame.jpeg)}
                encoded = json.dumps(header, separators=(',', ':')).encode()
                body = (struct.pack('<I', len(encoded)) + encoded + frame.jpeg
                        + frame.depth.astype('<f4', copy=False).tobytes() + frame.confidence.tobytes())
                if len(body) > 8 * 1024 * 1024:
                    raise HTTPException(413, 'RGB-D capture exceeds 8 MiB')
                self.cached_body, self.cached_key = body, key
            with self.state.lock:
                if generation != self.state.generation or not self.state.connected or h.get('_mapping_socket_generation') != self.state.socket_generation:
                    raise HTTPException(503, 'RGB-D scope changed during export')
            return self.cached_body, headers


def mount(app):
    """Explicit service-owner integration seam; uses existing /phone relay prefix."""
    capture = RawCapture(app.state.mapping)
    app.state.mapping.frame_observers.append(capture.observe)

    @app.get('/phone/frame.bin')
    async def frame(request: Request):
        body, headers = await asyncio.to_thread(capture.snapshot, request.query_params.get('after'))
        if body is None:
            return Response(status_code=204, headers=headers)
        headers = dict(headers)
        key, native_body = headers['X-Capture-Key'], body
        body, coding = await asyncio.to_thread(capture.encoded.get, 'raw',
            lambda: key, lambda: (key, native_body), request.headers.get('Accept-Encoding'))
        headers['Vary'] = 'Accept-Encoding'
        if coding:
            headers['Content-Encoding'] = coding
        return bulk_response(body, app.state.bulk,
                             media_type='application/octet-stream', headers=headers)

    # create_app may already have its catch-all static mount. Keep this specific
    # GET ahead of it when explicitly mounted by an integration/test caller.
    route = app.router.routes.pop()
    app.router.routes.insert(0, route)
    return capture
