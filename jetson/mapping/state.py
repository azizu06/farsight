"""Single-session map state; processing runs off the ASGI event loop."""

import threading
import time
from collections import OrderedDict
import cv2
import numpy as np

from .geometry import encode_points, export_ply
from .phone import reconstruct
from .surfaces import SurfaceMap
from .voxel import VoxelMap
from jetson.camera import adjusted_receipt


class MapState:
    def __init__(self, demo=False):
        self.lock = threading.RLock()
        self.cloud = VoxelMap()
        self.surfaces = SurfaceMap()
        self.demo = demo
        self.connected = demo
        self.identity = ("simulated-room", 1) if demo else None
        self.device = "Ray-cast room" if demo else "No sensor"
        self.generation = 0
        self.processing_generation = 0
        self.paused = False
        self.last_received = 0.0
        self.last_frame_received = 0.0
        self.frame_id = -1
        self.last_capture = -1.0
        self.last_pose_capture = -1.0
        self.pose = np.eye(4)
        self.tracking = "not_available"
        self.frames = self.rejected = self.dropped = 0
        self.revision = 0
        self.fps = self.processing_ms = 0.0
        self.reason = "Waiting for iPhone RGB-D stream"
        self.jpeg = self.depth_jpeg = None
        self.latest_camera = (None, 0.0, -1, "SIMULATED" if demo else "IPHONE LIDAR")
        self.depth_range = None
        self.pose_history = OrderedDict()
        self.scan = None

    def start_session(self, identity, device):
        with self.lock:
            if identity != self.identity:
                self.clear()
                self.identity = identity
                self.pose = np.eye(4)
                self.jpeg = self.depth_jpeg = None
                self.latest_camera = (None, 0.0, -1, "SIMULATED" if self.demo else "IPHONE LIDAR")
                self.depth_range = None
                self.last_received = 0.0
                self.last_frame_received = 0.0
                self.frame_id = -1
            self.connected, self.device = True, device
            self.processing_generation += 1
            self.last_capture = self.last_pose_capture = -1.0
            self.pose_history.clear()
            self.tracking = "not_available"
            self.reason = "Waiting for synchronized RGB-D frames"

    def clear(self):
        with self.lock:
            self.cloud.clear()
            self.surfaces.clear()
            self.generation += 1
            self.processing_generation += 1
            self.revision += 1
            self.frames = 0
            self.scan = None

    def install_scan(self, cloud, metadata):
        """Replace, never stitch separate rover origins or mix with ARKit geometry."""
        with self.lock:
            self.clear()
            self.cloud = cloud
            self.scan = metadata
            self.paused = True
            self.reason = "Camera-only snapshot retained; estimated metric depth"

    def observe_pose(self, header, transform):
        with self.lock:
            capture = header["t_capture"]
            identity = (header["frame_id"], header["tracking"], tuple(transform.ravel()))
            previous = self.pose_history.get(capture)
            if previous is not None and previous != identity:
                raise ValueError("Pose and RGB-D disagree about the same capture")
            self.pose_history[capture] = identity
            while len(self.pose_history) > 128:
                self.pose_history.popitem(last=False)
            if header["t_capture"] <= self.last_pose_capture:
                return
            self.last_pose_capture = header["t_capture"]
            self.last_received = time.monotonic()
            self.pose, self.tracking = transform, header["tracking"]

    def process(self, frame):
        started = time.monotonic()
        camera_receipt = adjusted_receipt(frame.header, frame.header.get("_received_monotonic", started))
        with self.lock:
            if not self.connected or (frame.header["session_id"], frame.header["map_epoch"]) != self.identity:
                return
            if frame.header["t_capture"] <= self.last_capture:
                self.rejected += 1
                return
            self.last_capture = frame.header["t_capture"]
            generation = self.processing_generation
            self.observe_pose(frame.header, frame.transform)
            self.jpeg = frame.jpeg
            self.last_frame_received = camera_receipt
            self.frame_id = frame.header["frame_id"]
            # Publish one immutable value. Safety/video readers must never wait
            # for the map lock while voxel or surface fusion is running.
            self.latest_camera = (self.jpeg, camera_receipt, self.frame_id,
                                  "SIMULATED" if self.demo else "IPHONE LIDAR")
            valid = np.isfinite(frame.depth) & (frame.depth > 0.15) & (frame.depth <= 8)
            scaled = np.where(valid, np.clip(frame.depth / 8 * 255, 0, 255), 0).astype(np.uint8)
            heat = cv2.applyColorMap(255 - scaled, cv2.COLORMAP_TURBO)
            heat[~valid] = 0
            _, encoded = cv2.imencode('.jpg', heat)
            self.depth_jpeg = encoded.tobytes()
            self.depth_range = [round(float(np.min(frame.depth[valid])), 2), round(float(np.max(frame.depth[valid])), 2)] if valid.any() else None
            may_fuse = (not self.paused and frame.header["tracking"] == "normal"
                        and self.tracking == "normal")
            self.reason = ("Fusion paused" if self.paused else "ARKit tracking limited; fusion frozen"
                           if not may_fuse else None)
        if not may_fuse:
            return
        points, colors, mesh = reconstruct(frame)
        with self.lock:
            if (generation != self.processing_generation or not self.connected
                    or self.paused or self.tracking != "normal"):
                return
            self.cloud.integrate(points, colors)
            self.surfaces.integrate(mesh, frame.transform)
            self.frames += 1
            self.revision += 1
            duration = time.monotonic() - started
            self.processing_ms = round(duration * 1000, 1)
            self.fps = round(1 / max(duration, 1e-4), 1)
            if not len(points):
                self.reason = "No confident depth samples in this frame"

    def status(self):
        with self.lock:
            age = time.monotonic() - self.last_received if self.last_received else None
            fresh = self.connected and age is not None and age < 2
            reason = ("Camera-only snapshot retained; estimated metric depth" if self.scan else
                      self.reason if fresh else "Sensor disconnected" if not self.connected else "Waiting for fresh sensor frames")
            pose = np.eye(4) if self.scan else self.pose
            return dict(type="status", source="SIMULATED" if self.demo else "CAMERA SCAN" if self.scan else "IPHONE LIDAR",
                        connected=self.connected, fresh=fresh, device=self.device,
                        session_id=self.identity[0] if self.identity else None,
                        map_epoch=self.identity[1] if self.identity else None,
                        generation=self.generation, revision=self.revision,
                        mesh_revision=self.surfaces.revision, paused=self.paused,
                        tracking=self.tracking if fresh else "stale",
                        points=len(self.cloud.cells), triangles=self.surfaces.count,
                        keyframes=len(self.surfaces.keyframes), frames=self.frames,
                        rejected=self.rejected, dropped=self.dropped,
                        processing_ms=self.processing_ms, processing_fps=self.fps,
                        age_ms=round(age * 1000) if age is not None else None,
                        position=pose[:3, 3].tolist(), transform=pose.flatten(order="F").tolist(), scan=self.scan,
                        voxel_m=self.cloud.size, depth_range=self.depth_range, reason=reason,
                        camera=fresh, detector_fps=0, rover=False, estop=True)

    def camera_snapshot(self):
        """Pose heartbeats never refresh the camera's capture receipt time."""
        return self.latest_camera

    def point_packet(self):
        with self.lock:
            return encode_points(*self.cloud.snapshot())

    def mesh_packet(self):
        with self.lock:
            return self.surfaces.encode()

    def ply(self):
        with self.lock:
            return export_ply(*self.cloud.snapshot())
