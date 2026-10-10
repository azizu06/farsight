"""H.264 fragmented MP4 transport over TCP; disconnect slow consumers."""

import asyncio
from contextlib import suppress
import shutil
import struct
import time
from collections import OrderedDict

import cv2
import numpy as np


def codec_from_init(data):
    offset = data.find(b"avcC")
    if offset < 0 or len(data) < offset + 8:
        raise ValueError("Encoder initialization is missing H.264 codec metadata")
    return "avc1." + data[offset + 5:offset + 8].hex().upper()


def fragment_timing(data, fps):
    """Our single video track uses a forced one-million-tick timescale."""
    offset = data.find(b"tfdt")
    samples = data.find(b"trun")
    if offset < 0 or samples < 0:
        raise ValueError("Fragment missing sample timing")
    version = data[offset + 4]
    start = struct.unpack_from(">Q" if version == 1 else ">I", data, offset + 8)[0] / 1000000
    count = struct.unpack_from(">I", data, samples + 8)[0]
    first = round(start * fps)
    return first, count, start, (first + count) / fps


def is_keyframe(data):
    """A fragment whose mdat carries an IDR picture, so a viewer can start decoding on it.

    ffmpeg writes MP4 samples as AVCC: 4-byte big-endian NAL lengths, not Annex B
    start codes, and default-base-is-moof says nothing about sync samples.
    (Ported from quest-hud's _is_keyframe on aziz/mac-dense-render.)
    """
    at = data.find(b"mdat")
    if at < 4:
        return False
    size = struct.unpack_from(">I", data, at - 4)[0]
    if size == 1:  # 64-bit largesize between kind and payload
        if at + 12 > len(data):
            return False
        size = struct.unpack_from(">Q", data, at + 4)[0]
        end = min(len(data), at - 4 + size)
        pos = at + 12
    else:
        end = min(len(data), at - 4 + size)
        pos = at + 4
    while pos + 4 <= end:
        length = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if length == 0 or pos + length > end:
            return False
        if data[pos] & 0x1F == 5:
            return True
        pos += length
    return False


class VideoService:
    def __init__(self, camera, enabled=False, width=640, height=480, fps=10, bitrate=500000):
        if (type(fps) is not int or not 1 <= fps <= 30 or type(bitrate) is not int or not 64000 <= bitrate <= 4000000
                or any(type(size) is not int or size % 2 or not 16 <= size <= 1920 for size in (width, height))):
            raise ValueError("Video needs even dimensions 16..1920, fps 1..30, bitrate 64000..4000000")
        self.camera, self.enabled = camera, enabled
        self.width, self.height, self.fps, self.bitrate = width, height, fps, bitrate
        self.process = None
        self.task = None
        self.init = None
        self.mime = None
        self.error = None
        self.subscribers = set()
        self.samples = OrderedDict()
        self.sample_id = 0
        self.encoded = 0
        self.encode_ms = None
        self.ingest_ms = None

    async def start(self):
        if self.enabled:
            if not shutil.which("ffmpeg"):
                self.error = "ffmpeg unavailable; use camera.jpg until installed"
                return
            self.task = asyncio.create_task(self._supervise())

    @property
    def ready(self):
        return bool(self.init and self.task and not self.task.done() and self.process
                    and self.process.returncode is None and not self.error)

    async def _supervise(self):
        while True:
            self.init = self.mime = None
            self.samples.clear()
            self.sample_id = 0
            self.error = None
            await self._run()
            self.init = self.mime = None
            await asyncio.sleep(.5)

    def subscribe(self):
        queue = asyncio.Queue(maxsize=2)
        queue.waiting_keyframe = True
        self.subscribers.add(queue)
        return queue

    def publish(self, fragment):
        for queue in tuple(self.subscribers):
            if isinstance(fragment, dict):
                keyframe = fragment.get("keyframe", True)
                if queue.waiting_keyframe and not keyframe:
                    continue
                if keyframe:
                    # Jump only at an independently decodable picture.
                    while not queue.empty():
                        queue.get_nowait()
                    queue.waiting_keyframe = False
            if queue.full():
                # Never drop dependent MP4 fragments and keep presenting them.
                self.subscribers.discard(queue)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            else:
                queue.put_nowait(fragment)

    async def _feed(self):
        while True:
            started = time.monotonic()
            frame = self.camera.snapshot()
            if frame and frame.age_ms < 500:
                image = await asyncio.to_thread(cv2.imdecode, np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
                if image is not None:
                    image = await asyncio.to_thread(cv2.resize, image, (self.width, self.height))
                    # JPEG decode may have overlapped a newer receipt; never queue
                    # an already stale picture into ffmpeg's input pipe.
                    if frame.age_ms >= 500:
                        continue
                    self.samples[self.sample_id] = (frame, time.monotonic())
                    self.sample_id += 1
                    while len(self.samples) > 600:
                        self.samples.popitem(last=False)
                    self.process.stdin.write(image.tobytes())
                    await asyncio.wait_for(self.process.stdin.drain(), timeout=.2)
            await asyncio.sleep(max(0, 1 / self.fps - (time.monotonic() - started)))

    async def _read(self):
        initialization = bytearray()
        fragment = bytearray()
        while True:
            header = await self.process.stdout.readexactly(8)
            size, kind = struct.unpack(">I4s", header)
            if not 8 <= size <= 4 * 1024 * 1024:
                raise ValueError("Invalid or excessive MP4 box from encoder")
            box = header + await self.process.stdout.readexactly(size - 8)
            if self.init is None:
                initialization.extend(box)
                if kind == b"moov":
                    self.init = bytes(initialization)
                    self.mime = f'video/mp4; codecs="{codec_from_init(self.init)}"'
            else:
                fragment.extend(box)
                if kind == b"mdat":
                    data = bytes(fragment)
                    first, count, start, end = fragment_timing(data, self.fps)
                    matched = [self.samples.pop(index) for index in range(first, first + count) if index in self.samples]
                    captures = [item[0] for item in matched]
                    if len(captures) != count or not captures:
                        raise ValueError("Encoded fragment cannot be matched to capture receipt times")
                    now = time.monotonic()
                    self.encode_ms = round((now - min(item[1] for item in matched)) * 1000, 2)
                    last = captures[-1]
                    self.ingest_ms = (round((last.published - last.socket_received) * 1000, 2)
                                      if last.published is not None and last.socket_received is not None else None)
                    self.encoded += count
                    self.publish(dict(payload=data, media_start_s=start, media_end_s=end,
                                      keyframe=is_keyframe(data), jetson_encode_ms=self.encode_ms,
                                      camera_ingest_ms=self.ingest_ms,
                                      received=min(frame.received for frame in captures),
                                      frame=captures[-1].frame_id, source=captures[-1].source,
                                      samples=[dict(media_start_s=(first + offset) / self.fps,
                                                    media_end_s=(first + offset + 1) / self.fps,
                                                    frame=frame.frame_id)
                                               for offset, frame in enumerate(captures)]))
                    fragment.clear()

    async def _run(self):
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-probesize", "32",
                "-analyzeduration", "0", "-f", "rawvideo",
                "-pix_fmt", "bgr24", "-s", f"{self.width}x{self.height}", "-r", str(self.fps),
                "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                "-tune", "zerolatency", "-threads", "1", "-bf", "0", "-profile:v", "baseline", "-level", "3.0",
                "-pix_fmt", "yuv420p", "-b:v", str(self.bitrate), "-maxrate", str(self.bitrate),
                # A full second of VBV allowed 30 KiB keyframe bursts at a
                # 500 kbps average. Keep 200 ms of rate-control buffer so the
                # slow link need not transmit a large stale keyframe at once.
                "-bufsize", str(max(64000, self.bitrate // 5)), "-g", str(max(1, self.fps // 4)), "-keyint_min", str(max(1, self.fps // 4)),
                "-sc_threshold", "0", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-video_track_timescale", "1000000",
                "-frag_duration", "1", "-flush_packets", "1", "-f", "mp4", "pipe:1"]
        tasks = []
        try:
            self.process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.PIPE,
                         stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            async def stderr():
                while line := await self.process.stderr.readline():
                    self.error = line.decode(errors="replace").strip()[:300]
            tasks = [asyncio.create_task(self._feed()), asyncio.create_task(self._read()), asyncio.create_task(stderr())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc) or type(exc).__name__
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
            if self.process and self.process.returncode is None:
                with suppress(ProcessLookupError):
                    self.process.terminate()
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.process.wait(), 2)
                if self.process.returncode is None:
                    self.process.kill()
                    await self.process.wait()
            self.publish(None)

    async def close(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
