"""Feed a RoverSim through the same mapping path as the --demo room.

Poses go to MapState.observe_pose at 30 Hz (like phone pose heartbeats) and
RGB-D frames to MapState.process at up to 10 Hz with one job in flight, so the
navigation bridge, voxel map and camera freshness all see simulated data the
way they see a LiDAR iPhone. Everything is labelled SIMULATED.
"""

import asyncio
import time

import numpy as np

from jetson.camera import CameraFrame
from jetson.mapping.phone import RGBDFrame

POSE_HZ = 30
FRAME_HZ = 10


class SimFeed:
    """Owns the sim's latest ground-truth detections for ControlState.detections."""

    def __init__(self, sim, identity=("simulated-room", 1)):
        self.sim = sim
        self.identity = identity
        self.started = time.monotonic()
        self.frame_id = 0
        self.boxes, self.boxes_at = [], None

    def header(self, now, pose):
        self.frame_id += 1
        return dict(version=1, type="frame", session_id=self.identity[0], map_epoch=self.identity[1],
                    frame_id=self.frame_id, t_capture=now - self.started, t_wall_ms=int(time.time() * 1000),
                    transform=pose.flatten(order="F").tolist(), tracking="normal")

    def frame(self, now):
        """Render one RGB-D frame; returns RGBDFrame (also refreshes detections)."""
        depth, rgb, pose = self.sim.render()
        boxes = self.sim.person_boxes(depth, pose)
        self.boxes, self.boxes_at = boxes, time.monotonic()
        confidence = np.where(depth > 0, 2, 0).astype(np.uint8)
        header = self.header(now, pose)
        header["_received_monotonic"] = now
        return RGBDFrame(header, rgb, depth, confidence, self.sim.calibration, pose, self.sim.jpeg(rgb))

    def detections(self):
        """The perception.snapshot() shape autonomy reads, from ground truth."""
        age = None if self.boxes_at is None else round((time.monotonic() - self.boxes_at) * 1000)
        fresh = age is not None and age < 1000
        return dict(boxes=list(self.boxes) if fresh else [], age_ms=age, fresh=fresh, error=None,
                    backend="simulated")


async def stream_world(state, camera, feed):
    fusion, next_frame = None, 0.0
    try:
        while True:
            now = time.monotonic()
            feed.sim.advance(now)
            if fusion is not None and fusion.done():
                fusion.result()
                fusion = None
            if fusion is None and now >= next_frame:
                # The frame carries its own pose (MapState.process observes it).
                frame = await asyncio.to_thread(feed.frame, now)
                camera.phone_rgb = CameraFrame(frame.jpeg, now, frame.header["frame_id"], "SIMULATED")
                fusion = asyncio.create_task(asyncio.to_thread(state.process, frame))
                next_frame = now + 1 / FRAME_HZ
            else:
                pose = feed.sim.camera_pose()
                state.observe_pose(feed.header(now, pose), pose)
            await asyncio.sleep(max(0, 1 / POSE_HZ - (time.monotonic() - now)))
    finally:
        if fusion is not None:
            await asyncio.shield(fusion)
