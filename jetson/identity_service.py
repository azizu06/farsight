"""Optional, read-only face identity inference on the latest camera frame."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import os
import time

from .identity_engine import Gallery, IdentityEngine, IdentityError


class IdentityService:
    MAX_AGE_MS = 1000

    def __init__(self, camera, detector_path=None, recognizer_path=None,
                 gallery_path=None, fps=3):
        self.camera = camera
        self.detector_path = detector_path
        self.recognizer_path = recognizer_path
        self.gallery_path = gallery_path
        self.fps = max(1, min(5, float(fps)))
        self.engine = None
        self.gallery = Gallery()
        self.error = None
        self.lock = asyncio.Lock()
        self.enrolling = False
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='face-identity')
        self.task = None
        self.packet = None
        self.binding = None
        self.previous = None
        self.closed = False

    @classmethod
    def from_environment(cls, camera):
        return cls(camera, os.getenv('FARSIGHT_IDENTITY_YUNET'),
                   os.getenv('FARSIGHT_IDENTITY_SFACE'),
                   os.getenv('FARSIGHT_IDENTITY_GALLERY'))

    async def _worker(self, fn, *args):
        # Keep the single-worker gate held even if the awaiting request cancels.
        # Releasing it while native inference continues would create a work queue.
        future = asyncio.get_running_loop().run_in_executor(self.executor, fn, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            with suppress(Exception):
                await asyncio.shield(future)
            raise

    def _setup(self):
        if self.gallery_path:
            self.gallery = Gallery(self.gallery_path)
        self.engine = IdentityEngine(self.detector_path, self.recognizer_path)

    async def start(self):
        if self.closed or self.task:
            return
        if not self.engine and not (self.detector_path or self.recognizer_path):
            return
        if not self.engine:
            try:
                if not (self.detector_path and self.recognizer_path):
                    raise IdentityError('Configure both YuNet and SFace models')
                await self._worker(self._setup)
            except Exception as exc:
                self.engine = None
                self.error = str(exc)[:240]
                return
        self.task = asyncio.create_task(self._run())

    def status(self):
        return dict(enabled=bool(self.engine or self.detector_path or self.recognizer_path),
                    ready=bool(self.engine and not self.closed), error=self.error,
                    people=len(self.gallery.people()), fps=self.fps,
                    gallery_generation=self.gallery.generation)

    def people(self):
        return self.gallery.people()

    async def enroll(self, name, image, person_id=None):
        if self.closed or self.engine is None:
            raise IdentityError('Face recognition is not ready')
        if self.enrolling:
            raise IdentityError('Face recognition is busy; retry in a moment')
        # One bounded priority waiter gets the next inference slot. All other
        # uploads fail busy, so a continuous camera cannot starve enrollment.
        self.enrolling = True
        acquired = False
        try:
            try:
                await asyncio.wait_for(self.lock.acquire(), 1)
                acquired = True
            except asyncio.TimeoutError as exc:
                raise IdentityError('Face recognition is busy; retry in a moment') from exc
            if self.closed:
                raise IdentityError('Face recognition is not ready')
            embedding = await self._worker(self.engine.enroll_embedding, image)
            record = await self._worker(self.gallery.enroll, name, embedding, person_id)
            self.packet = None
            return record
        finally:
            if acquired:
                self.lock.release()
            self.enrolling = False

    def remove(self, person_id):
        removed = self.gallery.remove(person_id)
        if removed:
            self.packet = None
        return removed

    async def process_once(self):
        if self.engine is None or self.closed or self.lock.locked() or self.enrolling:
            return
        frame = self.camera.snapshot()
        if frame is None or frame.age_ms >= self.MAX_AGE_MS:
            return
        identity = (frame.source, frame.frame_id, frame.received, id(frame.capture_binding))
        if identity == self.previous:
            return
        self.previous = identity
        generation = self.gallery.generation
        started = time.monotonic()
        async with self.lock:
            try:
                detected = await self._worker(self.engine.infer, frame.jpeg)
                faces = []
                for box in detected[:8]:
                    match = self.gallery.match(box['embedding'])
                    faces.append({key: box[key] for key in ('x', 'y', 'w', 'h', 'conf')} |
                                 (match or dict(name='Unknown', person_id=None, similarity=None)))
                # A removed enrollment cannot reappear from an in-flight result.
                if generation != self.gallery.generation:
                    faces = []
                self.error = None
            except Exception as exc:
                faces = []
                self.error = str(exc)[:240]
        self.binding = frame.capture_binding
        self.packet = dict(type='identity', source=frame.source, frame=frame.frame_id,
                           received=frame.received, faces=faces,
                           gallery_generation=generation,
                           infer_ms=round((time.monotonic() - started) * 1000, 1))

    def snapshot(self):
        packet = dict(self.packet or dict(type='identity', source=None, frame=None,
                      received=None, faces=[], gallery_generation=self.gallery.generation,
                      infer_ms=None))
        frame = self.camera.snapshot()
        age = (max(0, round((time.monotonic() - packet['received']) * 1000))
               if packet['received'] is not None else None)
        fresh = bool(not self.closed and frame and age is not None and age < self.MAX_AGE_MS
                     and frame.age_ms < self.MAX_AGE_MS and frame.source == packet['source']
                     and frame.frame_id >= packet['frame'] and frame.received >= packet['received']
                     and frame.capture_binding is self.binding
                     and (not frame.capture_verified or (self.binding and self.binding.active))
                     and packet['gallery_generation'] == self.gallery.generation)
        packet.update(age_ms=age, fresh=fresh, error=self.error)
        if not fresh:
            packet['faces'] = []
        return packet

    async def _run(self):
        while True:
            started = time.monotonic()
            await self.process_once()
            await asyncio.sleep(max(.01, 1 / self.fps - (time.monotonic() - started)))

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        self.packet = None
        async with self.lock:
            self.executor.shutdown(wait=False, cancel_futures=True)
