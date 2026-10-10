"""H.264 fragmented MP4 transport over TCP; disconnect slow consumers."""

import asyncio
from contextlib import suppress
import shutil
import struct
import subprocess
import time
from collections import OrderedDict

import cv2
import numpy as np

# JPEG /camera.jpg is not a profile knob. The viewer polls it at up to 10 req/s,
# so that encode stays at today's preview size in every profile (see docs/video-quality.md).
PROFILES = {
    "low": dict(width=640, height=480, fps=10, bitrate=500000,
                encoder="libx264", preset="ultrafast", profile="baseline", level="3.0"),
    "hq": dict(width=1280, height=720, fps=15, bitrate=1500000,
               encoder="libx264", preset="veryfast", profile="main", level="3.1"),
}
# Jetson hardware encoders, in preference order. A laptop ffmpeg has neither.
NVENC_ENCODERS = ("h264_nvmpi", "h264_nvv4l2enc")


def video_settings(profile="low", width=None, height=None, fps=None, bitrate=None):
    """Resolve a named profile, then let explicit width/height/fps/bitrate win."""
    name = (profile or "low").strip().lower()
    if name not in PROFILES:
        raise ValueError(f"Unknown video profile {profile!r}; expected low or hq")
    settings = dict(PROFILES[name])
    settings["profile_name"] = name
    for key, value in (("width", width), ("height", height), ("fps", fps), ("bitrate", bitrate)):
        if value is not None:
            settings[key] = value
    return settings


def ffmpeg_encoders(ffmpeg="ffmpeg", runner=None):
    run = runner or subprocess.run
    try:
        result = run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return ()
    if result.returncode != 0:
        return ()
    return tuple(line.split()[1] for line in result.stdout.splitlines()
                 if line.startswith(" V") and len(line.split()) > 1)


def choose_encoder(requested, available):
    """Use a hardware encoder only when this run asked for one and ffmpeg lists it.

    ``requested`` may be ``libx264``, one ``NVENC_ENCODERS`` name, or ``"nvenc"``
    (try h264_nvmpi, then h264_nvv4l2enc). Anything missing falls back to libx264.
    Detection never promotes an unrequested hardware encoder over software.
    Listing an encoder is not the same as it opening; a failed open still falls back.
    """
    names = NVENC_ENCODERS if requested == "nvenc" else (requested,)
    for name in names:
        if name in NVENC_ENCODERS and name in available:
            return name
    return "libx264"


def fit_frame(image, width, height):
    """Stretch the camera picture to fill the encode box. Same as the old resize.

    Detection boxes are positions in the camera picture. The viewer draws them
    across the whole video frame, so a padded frame puts every box on the black
    bars. Stretching keeps a box at the same fraction of the frame as the JPEG path.
    A 4:3 phone picture in the 16:9 hq box looks wider. That is the cost of boxes
    landing on the person instead of beside them.
    """
    if image.shape[1] == width and image.shape[0] == height:
        return image
    return cv2.resize(image, (width, height))


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
    def __init__(self, camera, enabled=False, width=None, height=None, fps=None, bitrate=None,
                 profile="low", encoder=None):
        settings = video_settings(profile, width, height, fps, bitrate)
        width, height, fps, bitrate = (settings[key] for key in ("width", "height", "fps", "bitrate"))
        if (type(fps) is not int or not 1 <= fps <= 30 or type(bitrate) is not int or not 64000 <= bitrate <= 4000000
                or any(type(size) is not int or size % 2 or not 16 <= size <= 1920 for size in (width, height))):
            raise ValueError("Video needs even dimensions 16..1920, fps 1..30, bitrate 64000..4000000")
        self.camera, self.enabled = camera, enabled
        self.profile = settings["profile_name"]
        self.width, self.height, self.fps, self.bitrate = width, height, fps, bitrate
        self.preset, self.h264_profile, self.level = settings["preset"], settings["profile"], settings["level"]
        # Stays libx264 until this process probes ffmpeg. /health does not report
        # the encoder name; readers of this object see the name that will run.
        self.requested_encoder = encoder or settings["encoder"]
        self.encoder = "libx264"
        self._encoders_known = None
        # True only after this process's hardware encoder has produced an init.
        # Cleared on every restart, so a chip that opened once and then dies
        # still falls back instead of being retried forever.
        self._encoder_opened = False
        self._encoder_failed = False
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
            self._encoder_opened = False
            await self._run()
            self.init = self.mime = None
            await asyncio.sleep(.5)

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
            started = time.monotonic()
            frame = self.camera.snapshot()
            if frame and frame.age_ms < 500:
                image = await asyncio.to_thread(cv2.imdecode, np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
                if image is not None:
                    image = await asyncio.to_thread(fit_frame, image, self.width, self.height)
                    self.samples[self.sample_id] = frame
                    self.sample_id += 1
                    while len(self.samples) > 600:
                        self.samples.popitem(last=False)
                    self.process.stdin.write(image.tobytes())
                    await asyncio.wait_for(self.process.stdin.drain(), timeout=2)
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
                    self._encoder_opened = True
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
                                      frame=captures[-1].frame_id, source=captures[-1].source,
                                      samples=[dict(media_start_s=(first + offset) / self.fps,
                                                    media_end_s=(first + offset + 1) / self.fps,
                                                    frame=frame.frame_id)
                                               for offset, frame in enumerate(captures)]))
                    fragment.clear()

    def encoder_args(self, encoder):
        # A full second of VBV allowed 30 KiB keyframe bursts at a
        # 500 kbps average. Keep 200 ms of rate-control buffer so the
        # slow link need not transmit a large stale keyframe at once.
        rate = ["-pix_fmt", "yuv420p", "-b:v", str(self.bitrate), "-maxrate", str(self.bitrate),
                "-bufsize", str(max(64000, self.bitrate // 5)), "-g", str(self.fps),
                "-keyint_min", str(self.fps)]
        if encoder == "libx264":
            return ["-c:v", "libx264", "-preset", self.preset, "-tune", "zerolatency",
                    "-profile:v", self.h264_profile, "-level", self.level, *rate]
        # Hardware encoders do not take x264 preset/tune. Peak rate and IDR
        # spacing stay the same so a fallback does not change the wire budget.
        return ["-c:v", encoder, *rate]

    async def _run(self):
        # The encoder list is only needed when this run asked for a hardware
        # encoder. A plain libx264 start does not need it, so skip the probe.
        if self.requested_encoder != "libx264" and self._encoders_known is None:
            self._encoders_known = await asyncio.to_thread(ffmpeg_encoders)
        # A listed hardware encoder that already failed to open stays off. Retrying
        # it would crash-loop the supervisor with no path back to libx264.
        encoder = "libx264" if self._encoder_failed else choose_encoder(
            self.requested_encoder, self._encoders_known or ())
        self.encoder = encoder
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "rawvideo",
                "-pix_fmt", "bgr24", "-s", f"{self.width}x{self.height}", "-r", str(self.fps),
                "-i", "pipe:0", "-an", *self.encoder_args(encoder),
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
            # Never opened means the chip refused us. Fall back instead of
            # crash-restarting the same dead encoder forever. Keep ffmpeg's
            # text so the reason the chip refused is not thrown away.
            if encoder != "libx264" and not self._encoder_opened:
                self._encoder_failed = True
                reason = f": {self.error}" if self.error else ""
                self.error = f"{encoder} failed to open; falling back to libx264{reason}"
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
