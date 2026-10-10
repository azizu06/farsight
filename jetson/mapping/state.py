"""Single-session map state; processing runs off the ASGI event loop."""

import threading
import time
from collections import OrderedDict
import cv2
import numpy as np

from .geometry import encode_points, export_ply
from .phone import VISUAL_LODS, reconstruct
from .dense import DenseMap
from .dense_worker import DenseWorker
from .surfaces import SurfaceMap
from .voxel import VoxelMap
from .wire import decimate_mesh_bin, decimate_points_bin
from jetson.camera import adjusted_receipt, capture_diagnostics

FUSION_BYPASSED = "Geometry lane external; fusion bypassed"


class MapState:
    def __init__(self, demo=False, fusion_bypass=False):
        self.lock = threading.RLock()
        # Bypass only coarse voxel/mesh fusion. Frames still publish the camera,
        # depth, pose and navigation observations, and retain measured dense detail.
        self.fusion_bypass = fusion_bypass
        self.cloud = VoxelMap()
        self.dense = DenseMap(cache_only=True)
        self.dense_worker = DenseWorker(lambda generation, cloud:
            generation == self.processing_generation and cloud is self.dense
            and self.connected and not self.paused and self.tracking == "normal")
        self.surfaces = SurfaceMap()
        self.demo = demo
        self.connected = demo
        self.identity = ("simulated-room", 1) if demo else None
        self.dense.source = list(self.identity) if self.identity else None
        self.device = "Ray-cast room" if demo else "No sensor"
        self.generation = 0
        self.processing_generation = 0
        self.socket_generation = 0  # socket lifetime; a visual map reset preserves the live socket
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
        self.capture_diagnostics = {}
        self.latest_camera = (None, 0.0, -1, "SIMULATED" if demo else "IPHONE LIDAR", False, None)
        self.depth_range = None
        self.pose_history = OrderedDict()
        self.scan = None
        # Visual wire caps (0 = uncapped): bound only the served/exported points.bin,
        # mesh.bin and export.ply; fusion, Nav2 depth/occupancy and labels stay full.
        self.points_cap = self.triangles_cap = 0
        # Called with every accepted RGB-D frame before fusion (navigation costmaps
        # need the newest depth, not only fused geometry). Must be cheap: keep a
        # reference and return.
        self.frame_observers = []

    def start_session(self, identity, device):
        with self.lock:
            if identity != self.identity:
                self.clear()
                self.identity = identity
                self.pose = np.eye(4)
                self.jpeg = self.depth_jpeg = None
                self.capture_diagnostics = {}
                self.latest_camera = (None, 0.0, -1, "SIMULATED" if self.demo else "IPHONE LIDAR", False, None)
                self.depth_range = None
                self.last_received = 0.0
                self.last_frame_received = 0.0
                self.frame_id = -1
            self.connected, self.device = True, device
            self.dense.source = list(identity)
            self.processing_generation += 1
            self.socket_generation += 1
            self.last_capture = self.last_pose_capture = -1.0
            self.pose_history.clear()
            self.tracking = "not_available"
            self.reason = "Waiting for synchronized RGB-D frames"
            return self.socket_generation

    def clear(self):
        with self.lock:
            self.cloud.clear()
            # A running visual job retains the old cloud, never the new epoch.
            # Swapping avoids waiting for its integration/delta lock on reset.
            self.dense = DenseMap(cache_only=True)
            self.dense.source = list(self.identity) if self.identity else None
            if self.scan:
                self.cloud = VoxelMap()  # restore phone fusion's voxel size/budget
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

    def observe_pose(self, header, transform, received_at=None, *, socket_generation=None):
        with self.lock:
            if (not self.connected or (header.get("session_id"), header.get("map_epoch")) != self.identity
                    or (socket_generation is not None and socket_generation != self.socket_generation)):
                return  # a cancelled socket worker cannot publish into a later session
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
            # CPU processing must not renew the age of an old pose capture.
            self.last_received = time.monotonic() if received_at is None else received_at
            self.pose, self.tracking = transform, header["tracking"]

    def process(self, frame, permits=None, *, socket_generation=None):
        started = time.monotonic()
        camera_receipt = adjusted_receipt(frame.header, frame.header.get("_received_monotonic", started))
        # Validate against this socket's live issuer, after decode/queue delay.
        # No producer-supplied verified flag can substitute for these checks.
        origin = permits.verified_origin(frame.header) if permits is not None else None
        if origin is not None:
            camera_receipt = min(camera_receipt, origin)
        with self.lock:
            if (not self.connected or (frame.header["session_id"], frame.header["map_epoch"]) != self.identity
                    or (socket_generation is not None and socket_generation != self.socket_generation)):
                return
            if frame.header["t_capture"] <= self.last_capture:
                self.rejected += 1
                return
            self.last_capture = frame.header["t_capture"]
            # Bind observers to this accepted socket lifetime. Overwrite any producer field:
            # an observer can finish after a reconnect even when sensor identity is reused.
            frame.header["_mapping_socket_generation"] = self.socket_generation
            generation = self.processing_generation
            self.observe_pose(frame.header, frame.transform, camera_receipt, socket_generation=socket_generation)
            self.jpeg = frame.jpeg
            self.capture_diagnostics = capture_diagnostics(frame.header)
            self.last_frame_received = camera_receipt
            self.frame_id = frame.header["frame_id"]
            # Publish one immutable value. Safety/video readers must never wait
            # for the map lock while voxel or surface fusion is running.
            self.latest_camera = (self.jpeg, camera_receipt, self.frame_id,
                                  "SIMULATED" if self.demo else "IPHONE LIDAR",
                                  origin is not None, permits if origin is not None else None)
            valid = np.isfinite(frame.depth) & (frame.depth > 0.15) & (frame.depth <= 8)
            scaled = np.where(valid, np.clip(frame.depth / 8 * 255, 0, 255), 0).astype(np.uint8)
            heat = cv2.applyColorMap(255 - scaled, cv2.COLORMAP_TURBO)
            heat[~valid] = 0
            _, encoded = cv2.imencode('.jpg', heat)
            self.depth_jpeg = encoded.tobytes()
            self.depth_range = [round(float(np.min(frame.depth[valid])), 2), round(float(np.max(frame.depth[valid])), 2)] if valid.any() else None
            may_fuse = (not self.fusion_bypass and not self.paused and frame.header["tracking"] == "normal"
                        and self.tracking == "normal")
            may_dense = (not self.paused and frame.header["tracking"] == "normal"
                         and self.tracking == "normal")
            dense_cloud = self.dense
            self.reason = (FUSION_BYPASSED if self.fusion_bypass else "Fusion paused" if self.paused
                           else "ARKit tracking limited; fusion frozen" if not may_fuse else None)
        for observer in self.frame_observers:
            try:
                observer(frame)
            except Exception:
                pass  # navigation must never break mapping or camera publication
        if may_dense:
            self.dense_worker.offer(frame, dense_cloud, generation)
        if not may_fuse:
            duration = time.monotonic() - started
            self.processing_ms = round(duration * 1000, 1)
            self.fps = round(1 / max(duration, 1e-4), 1)
            return
        points, colors, mesh, lods = reconstruct(frame, lods=VISUAL_LODS)
        with self.lock:
            if (generation != self.processing_generation or not self.connected
                    or self.paused or self.tracking != "normal"):
                return
            self.cloud.integrate(points, colors)
            self.surfaces.integrate(mesh, frame.transform, lods)
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
                        mesh_revision=self.surfaces.revision, paused=self.paused, fusion_bypass=self.fusion_bypass,
                        dense_worker=self.dense_worker.status(),
                        tracking=self.tracking if fresh else "stale",
                        points=len(self.cloud.cells), triangles=self.surfaces.count,
                        keyframes=len(self.surfaces.keyframes), frames=self.frames,
                        capped=self.visual_capped(),
                        rejected=self.rejected, dropped=self.dropped,
                        processing_ms=self.processing_ms, processing_fps=self.fps,
                        age_ms=round(age * 1000) if age is not None else None,
                        position=pose[:3, 3].tolist(), transform=pose.flatten(order="F").tolist(), scan=self.scan,
                        voxel_m=self.cloud.size, depth_range=self.depth_range, reason=reason,
                        camera=fresh, detector_fps=0, rover=False, estop=True)

    def camera_snapshot(self):
        """Pose heartbeats never refresh the camera's capture receipt time."""
        return self.latest_camera[:4]

    def lidar_drive_eligible(self):
        """Live /phone session with normal ARKit tracking; lock-free for safety readers."""
        return self.connected and not self.demo and self.tracking == "normal"

    def camera_capture_snapshot(self):
        """Immutable camera and issuer evidence, independent of fusion locks."""
        value = self.latest_camera
        return value if len(value) == 6 else (*value, False, None)

    def visual_capped(self):
        """True when a wire cap is currently decimating points or mesh triangles."""
        with self.lock:
            return ((0 < self.points_cap < len(self.cloud.cells))
                    or (0 < self.triangles_cap < self.surfaces.count))

    def point_packet(self):
        with self.lock:
            return decimate_points_bin(encode_points(*self.cloud.snapshot()), self.points_cap)

    def mesh_packet(self):
        with self.lock:
            if 0 < self.triangles_cap < self.surfaces.count:
                meshes = self.surfaces.visual(self.triangles_cap)
                if meshes is not None:
                    return self.surfaces.pack(meshes)
            # Keyframes without stored LODs keep the stride fallback (Rust parity).
            return decimate_mesh_bin(self.surfaces.encode(), self.triangles_cap)

    def ply(self):
        """Follows the capped points, like the Rust server's PLY derived from points.bin."""
        with self.lock:
            points, colors = self.cloud.snapshot()
        n, cap = len(points), self.points_cap
        if 0 < cap < n:
            keep = np.arange(cap, dtype=np.int64) * n // cap
            points, colors = points[keep], colors[keep]
        return export_ply(points, colors)
