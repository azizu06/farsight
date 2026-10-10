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


class VideoService:
    def __init__(self, camera, enabled=False, width=640, height=480, fps=15, bitrate=1000000):
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

    async def start(self):
        if self.enabled:
            if not shutil.which("ffmpeg"):
                self.error = "ffmpeg unavailable; use camera.jpg until installed"
                return
            self.task = asyncio.create_task(self._run())

    def subscribe(self):
        queue = asyncio.Queue(maxsize=8)
        self.subscribers.add(queue)
        return queue

    def publish(self, fragment):
        for queue in tuple(self.subscribers):
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
            frame = self.camera.snapshot()
            if frame and frame.age_ms < 500:
                image = await asyncio.to_thread(cv2.imdecode, np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
                if image is not None:
                    image = cv2.resize(image, (self.width, self.height))
                    self.samples[self.sample_id] = frame
                    self.sample_id += 1
                    while len(self.samples) > 600:
                        self.samples.popitem(last=False)
                    self.process.stdin.write(image.tobytes())
                    await asyncio.wait_for(self.process.stdin.drain(), timeout=2)
            await asyncio.sleep(1 / self.fps)

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
                    captures = [self.samples[index] for index in range(first, first + count) if index in self.samples]
                    if len(captures) != count or not captures:
                        raise ValueError("Encoded fragment cannot be matched to capture receipt times")
                    self.publish(dict(payload=data, media_start_s=start, media_end_s=end,
                                      received=min(frame.received for frame in captures),
                                      frame=captures[-1].frame_id, source=captures[-1].source))
                    fragment.clear()

    async def _run(self):
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
                "-pix_fmt", "bgr24", "-s", f"{self.width}x{self.height}", "-r", str(self.fps),
                "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                "-tune", "zerolatency", "-profile:v", "baseline", "-level", "3.0",
                "-pix_fmt", "yuv420p", "-b:v", str(self.bitrate), "-maxrate", str(self.bitrate),
                "-bufsize", str(self.bitrate), "-g", str(self.fps), "-keyint_min", str(self.fps),
                "-sc_threshold", "0", "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "-video_track_timescale", "1000000",
                "-frag_duration", "100000", "-f", "mp4", "pipe:1"]
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
