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
        self.pending = self.capture = None  # (RGB-D frame, its boxes, original receipt)

    def header(self, now, pose):
        self.frame_id += 1
        return dict(version=1, type="frame", session_id=self.identity[0], map_epoch=self.identity[1],
                    frame_id=self.frame_id, t_capture=now - self.started, t_wall_ms=int(time.time() * 1000),
                    transform=pose.flatten(order="F").tolist(), tracking="normal")

    def frame(self, now):
        """Render one pending RGB-D capture; boxes publish after MapState accepts it."""
        depth, rgb, pose = self.sim.render()
        boxes = self.sim.person_boxes(depth, pose)
        confidence = np.where(depth > 0, 2, 0).astype(np.uint8)
        header = self.header(now, pose)
        header["_received_monotonic"] = now
        frame = RGBDFrame(header, rgb, depth, confidence, self.sim.calibration, pose, self.sim.jpeg(rgb))
        self.pending = frame, boxes, now
        return frame

    def offer_frame(self, frame):
        """Publish an accepted capture after navigation's depth observer received it."""
        pending = self.pending
        if pending is not None and pending[0] is frame and frame.header.get("_mapping_sensor_key") is not None:
            self.capture = pending

    def detections(self):
        """The perception.snapshot() shape autonomy reads, from ground truth."""
        capture = self.capture
        frame, boxes, received = capture if capture is not None else (None, [], None)
        elapsed = None if received is None else time.monotonic() - received
        age = None if elapsed is None else round(elapsed * 1000)
        # MapState stamps this exact frame only after acceptance. Never guess a
        # current socket/capture key for boxes rendered earlier or still in flight.
        key = frame.header.get("_mapping_sensor_key") if frame is not None else None
        fresh = key is not None and elapsed is not None and 0 <= elapsed < 1
        return dict(boxes=list(boxes) if fresh else [], age_ms=age, fresh=fresh, error=None,
                    backend="simulated", source="SIMULATED", mirrored=False, sensor_key=key,
                    frame=frame.header["frame_id"] if frame is not None else -1)


async def stream_world(state, camera, feed):
    fusion, pose_worker, next_frame = None, None, 0.0
    # Existing depth observers (including Nav) run before detections publish.
    state.frame_observers.append(feed.offer_frame)
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
                # Fusion holds the map lock while integrating. Waiting for it
                # here must not also block camera/control's event loop. Keep
                # one awaited pose job, with the receipt and socket lifetime
                # captured before its executor wait, never a queued backlog.
                socket_generation = state.socket_generation
                pose_worker = asyncio.create_task(asyncio.to_thread(
                    state.observe_pose, feed.header(now, pose), pose,
                    received_at=now, socket_generation=socket_generation))
                await asyncio.shield(pose_worker)
                pose_worker = None
            await asyncio.sleep(max(0, 1 / POSE_HZ - (time.monotonic() - now)))
    finally:
        try:
            workers = [worker for worker in (pose_worker, fusion) if worker is not None]
            if workers:
                # Cancellation cannot stop an executor job. Repeated cancel
                # requests must not remove observers while either job still
                # uses them, even when another worker raises an exception.
                drained = asyncio.gather(*workers, return_exceptions=True)
                while not drained.done():
                    try:
                        await asyncio.shield(drained)
                    except asyncio.CancelledError:
                        continue
                for result in drained.result():
                    if isinstance(result, BaseException):
                        raise result
        finally:
            state.frame_observers.remove(feed.offer_frame)
