"""One stopped-position camera sweep, using the operator's existing control lease."""

import asyncio
from contextlib import suppress
import time
import cv2
import numpy as np

from jetson.depth import MetricDepth, back_project, intrinsics_from_fov
from jetson.mapping.demo import simulated_frame
from jetson.mapping.voxel import VoxelMap


class ScanService:
    YAWS = (-80, -40, 0, 40, 80)

    def __init__(self, mapping, camera, control, perception, *, model=None,
                 physical_pan=False, horizontal_fov=70, intrinsics=None, turn_dps=90):
        if not np.isfinite(turn_dps) or not 20 <= turn_dps <= 180:
            raise ValueError("Scan turn calibration must be 20..180 degrees/second")
        intrinsics_from_fov(640, 480, horizontal_fov)
        self.mapping, self.camera, self.control, self.perception = mapping, camera, control, perception
        self.depth = MetricDepth(model) if model else None
        self.physical_pan, self.fov, self.intrinsics, self.turn_dps = physical_pan, horizontal_fov, intrinsics, turn_dps
        self.task = None
        self.result = dict(state="idle", running=False)

    def start(self, session_id, permit):
        if self.task and not self.task.done():
            raise ValueError("A scan is already running")
        if not self.depth and not self.mapping.demo:
            raise ValueError("Configure --depth-model for a camera-only scan")
        frame = self.camera.snapshot()
        if frame is None or frame.age_ms >= 500 or frame.source == "IPHONE LIDAR":
            raise ValueError("Scan requires fresh camera-only RGB (LiDAR uses the existing mapper)")
        token = self.control.state.begin_scan(session_id, permit)
        self.result = dict(state="sweeping", running=True, simulated=self.mapping.demo,
                           approximate=not self.physical_pan, views=[], points=0, markers=[],
                           backend=self.depth.backend if self.depth else "synthetic-depth")
        self.task = asyncio.create_task(self.run(token))
        return self.result.copy()

    async def wait(self, token, seconds, *, w=0, pan=0):
        until = time.monotonic() + seconds
        while True:
            self.control.state.scan_command(token, w=w, pan=pan)
            remaining = until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(.02, remaining))

    async def capture(self, token, yaw, index):
        if self.mapping.demo:
            frame = await asyncio.to_thread(simulated_frame, 0, index, scan_yaw=yaw)
            k = frame.calibration
            return cv2.cvtColor(frame.rgb, cv2.COLOR_RGB2BGR), frame.depth, (k.fx, k.fy, k.cx, k.cy), None
        # A frame received before the settled stop must not become this view.
        after = time.monotonic()
        while time.monotonic() - after < 1.5:
            self.control.state.scan_command(token, pan=yaw if self.physical_pan else 0)
            frame = self.camera.snapshot()
            if frame and frame.received >= after and frame.age_ms < 500:
                bgr = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
                if bgr is None:
                    raise ValueError("Scan camera JPEG decode failed")
                k = self.intrinsics or intrinsics_from_fov(bgr.shape[1], bgr.shape[0], self.fov)
                return bgr, None, k, frame
            await asyncio.sleep(.02)
        raise ValueError("No fresh camera frame after sweep settled")

    async def infer(self, bgr):
        # On cancellation, drain the thread before closing a CUDA context.
        task = asyncio.create_task(asyncio.to_thread(self.depth.predict, bgr))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await task
            raise

    async def run(self, token):
        started = time.monotonic()
        cloud = VoxelMap(size=.025, capacity=100_000)
        previous = 0
        try:
            async with asyncio.timeout(90):
                for index, yaw in enumerate(self.YAWS):
                    if not self.mapping.demo:
                        if self.physical_pan:
                            await self.wait(token, .4, pan=yaw)
                        else:
                            # Open-loop motor timing is explicitly approximate;
                            # bounded steps, fresh range, held operator permits.
                            delta = yaw - previous
                            await self.wait(token, abs(delta) / self.turn_dps, w=.3 if delta > 0 else -.3)
                            await self.wait(token, .3)
                    self.control.state.scan_command(token, pan=yaw if self.physical_pan else 0)
                    bgr, synthetic, k, frame = await self.capture(token, yaw, index)
                    at = time.monotonic()
                    depth = await self.infer(bgr) if self.depth else synthetic
                    latency = (time.monotonic() - at) * 1000
                    self.control.state.scan_command(token, pan=yaw if self.physical_pan else 0)
                    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                    xyz, colors = await asyncio.to_thread(back_project, depth, rgb, k, yaw)
                    await asyncio.to_thread(cloud.integrate, xyz, colors)
                    self.collect_markers(frame, depth, rgb, k, yaw)
                    self.result["views"].append(dict(yaw_deg=yaw, depth_ms=round(latency, 2), samples=len(xyz)))
                    previous = yaw
                self.control.state.scan_command(token, pan=previous if self.physical_pan else 0)
                self.result.update(state="complete", running=False, points=len(cloud.cells),
                                   scan_ms=round((time.monotonic() - started) * 1000, 2))
                self.mapping.install_scan(cloud, self.result.copy())
        except (Exception, asyncio.CancelledError) as exc:
            self.result.update(state="aborted", running=False, error=str(exc) or "Server shutdown",
                               scan_ms=round((time.monotonic() - started) * 1000, 2))
        finally:
            self.control.state.end_scan(token)

    def collect_markers(self, frame, depth, rgb, k, yaw):
        packet = self.perception.snapshot()
        if (frame is None or not packet.get("fresh") or packet.get("error")
                or packet.get("source") != frame.source or packet.get("frame") != frame.frame_id):
            return
        h, w = depth.shape
        for box in packet.get("boxes", [])[:100]:
            u = min(w - 1, max(0, int((box["x"] + box["w"] / 2) * w)))
            v = min(h - 1, max(0, int((box["y"] + box["h"] / 2) * h)))
            # Project one box-centre sample through the same pinhole/yaw seam.
            fx, fy, cx, cy = k
            xyz, _ = back_project(depth[v:v+1, u:u+1], rgb[v:v+1, u:u+1], (fx, fy, cx-u, cy-v), yaw)
            if len(xyz):
                self.result["markers"].append(dict(id=box.get("id"), cls=box["cls"], position=xyz[0].tolist()))

    async def close(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await self.task
        if self.depth:
            await asyncio.to_thread(self.depth.close)
