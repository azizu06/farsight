"""Navigation seam between jetson.server and the ROS 2 Nav2 stack (ros2/farsight_nav).

The server stays the only authority over motors. The ROS bridge node connects
to the loopback-only WebSocket ``/ws/nav``; the server streams the phone pose,
depth frames and range to it and tells it what the operator wants (``intent``).
The bridge runs Nav2 and streams ``cmd_vel`` and navigation status back. A
velocity command only moves the rover while ControlState sees a live
held-deadman lease in an autonomy mode; stale commands (> 250 ms) stop it, and
a missing bridge falls back to the reactive jetson.autonomy policy.

Frames (docs/navigation.md): ARKit/voxel-map world is metres with Y up and the
rover starting toward -Z. ROS odom is X forward, Y left, Z up, so
``X = -z, Y = -x, Z = y - floor_y``. Viewer-facing coordinates are always the
voxel-map ``x, z``.
"""

import asyncio
from contextlib import suppress
import json
import math
import struct
import threading
import time

import numpy as np
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel

PROTOCOL = 1
STATES = ("idle", "planning", "driving", "recovering", "arrived", "failed", "unavailable")
NAV_MODES = ("explore", "seek", "goto")
LOOPBACK = ("127.0.0.1", "::1", "localhost", "testclient")


def world_to_odom(x, z):
    """Voxel-map world (x, z) to ROS odom (X, Y)."""
    return -z, -x


def odom_to_world(X, Y):
    """ROS odom (X, Y) to voxel-map world (x, z)."""
    return -Y, -X


def simplify(points, tolerance=0.03, limit=200):
    """Douglas-Peucker polyline reduction, then a uniform cap that keeps both ends."""
    points = [tuple(p) for p in points]
    if len(points) <= 2:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        (ax, ay), (bx, by) = points[first], points[last]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        worst, index = -1.0, None
        for i in range(first + 1, last):
            px, py = points[i]
            distance = (abs(dy * (px - ax) - dx * (py - ay)) / norm if norm > 1e-9
                        else math.hypot(px - ax, py - ay))
            if distance > worst:
                worst, index = distance, i
        if index is not None and worst > tolerance:
            keep[index] = True
            stack += [(first, index), (index, last)]
    reduced = [p for p, k in zip(points, keep) if k]
    if len(reduced) > limit:
        step = (len(reduced) - 1) / (limit - 1)
        reduced = [reduced[round(i * step)] for i in range(limit)]
    return reduced


def encode_depth(frame, *, floor_y, source, age_ms, generation=0, max_width=160, max_range=6.0):
    """uint32 header length, JSON header, then little-endian uint16 millimetres (0 = no return).

    Low-confidence samples are dropped here so the bridge publishes measured
    points only. Intrinsics are rescaled for the subsampled grid.
    """
    depth = np.asarray(frame.depth, np.float32)
    stride = max(1, math.ceil(depth.shape[1] / max_width))
    valid = np.isfinite(depth) & (depth > 0.15) & (depth <= max_range)
    confidence = getattr(frame, "confidence", None)
    if confidence is not None:
        valid &= np.asarray(confidence) >= 1
    sampled = np.where(valid, depth, 0)[::stride, ::stride]
    millimetres = np.round(sampled * 1000).astype("<u2")
    k = frame.calibration
    header = dict(type="depth", version=PROTOCOL, frame_id=int(frame.header.get("frame_id", 0)),
                  width=int(millimetres.shape[1]), height=int(millimetres.shape[0]),
                  fx=k.fx / stride, fy=k.fy / stride, cx=k.cx / stride, cy=k.cy / stride,
                  transform=np.asarray(frame.transform, float).flatten(order="F").tolist(),
                  floor_y=floor_y, source=source, age_ms=age_ms, generation=generation,
                  tracking=frame.header.get("tracking"))
    data = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack("<I", len(data)) + data + millimetres.tobytes()


def decode_depth(packet):
    """Inverse of encode_depth: (header, float32 metres) for tests and the bridge."""
    length = struct.unpack_from("<I", packet)[0]
    header = json.loads(packet[4:4 + length])
    raw = np.frombuffer(packet, "<u2", header["width"] * header["height"], 4 + length)
    return header, raw.reshape(header["height"], header["width"]).astype(np.float32) / 1000


def empty_nav(state, message):
    """A nav object with no backend (field-for-field with the Rust server)."""
    return dict(state=state, mode=None, goal=None, path=[], frontiers=[], backend=None, message=message)


SENSOR_MESSAGES = {
    "nav_pose_lost": "No fresh ARKit pose; navigation stopped",
    "nav_tracking_limited": "ARKit tracking limited; navigation stopped",
    "nav_depth_lost": "No fresh LiDAR depth; navigation stopped",
    "nav_sensor_reset": "Sensor session changed; navigation reset",
}


class Refused(Exception):
    """POST /nav/goal refusal: 409 {"nav": <nav object>, "message": reason}."""

    def __init__(self, nav, message):
        super().__init__(message)
        self.nav, self.message = nav, message


class GoalRequest(BaseModel):
    x: float
    z: float
    session_id: str | None = None
    permit: str | None = None


class NavService:
    CMD_TTL = 0.25     # a velocity command older than one control lease stops the rover
    BRIDGE_TTL = 1.5   # no bridge status for this long: Nav2 is unavailable, use the fallback
    POSE_TTL = 0.5     # Nav2 motion needs a normal-tracking ARKit pose at least this fresh ...
    DEPTH_TTL = 1.0    # ... and measured depth of the same sensor session at least this fresh

    def __init__(self, mapping, *, speed_mps=0.45, turn_radps=3.0, max_v=0.7, max_w=0.8,
                 camera_height=0.15, backed_modes=NAV_MODES, standoff=0.8, clock=time.monotonic):
        for name, value in (("speed_mps", speed_mps), ("turn_radps", turn_radps), ("max_v", max_v),
                            ("max_w", max_w), ("camera_height", camera_height)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Navigation {name} must be positive")
        self.mapping, self.clock = mapping, clock
        self.speed_mps, self.turn_radps = speed_mps, turn_radps
        self.max_v, self.max_w = min(max_v, 1.0), min(max_w, 1.0)
        self.camera_height = camera_height
        self.backed_modes = frozenset(backed_modes)
        self.standoff = standoff
        # Seek: the tracked person's floor position from LiDAR depth, and the standoff goal
        # that follows it. seek_goal_id is a string ("seek-N") so it never matches goto ids.
        self.seek = None            # dict(target_id, person, seen_at, goal, goal_id, state, distance)
        self.seek_seq = 0
        self.seek_checked = 0.0
        self.control = None
        self.lock = threading.RLock()
        self.bridge = None
        self.bridge_seen = -math.inf
        self.bridge_hello = None
        self.cmd = None             # (v, w normalized, received monotonic, linear m/s, angular rad/s CCW)
        self.remote = {}            # latest bridge status
        self.mode, self.target_id, self.active = None, None, False
        self.goal = None            # voxel-map (x, z)
        self.goal_id = 0
        self.frame = None           # newest normal-tracking RGB-D frame of the current sensor session
        self.frame_at = None        # monotonic receipt of self.frame
        self.frame_seq = 0
        # Sensor generation: bumps whenever MapState's (session_id, map_epoch) changes. Old
        # goals, commands and depth never carry across, and the bridge resets its ROS state.
        self.identity = mapping.identity if mapping is not None else None
        self.generation = 0
        self.router = APIRouter()
        self.router.add_api_websocket_route("/ws/nav", self.websocket)
        self.router.add_api_route("/nav/goal", self.post_goal, methods=["POST"], status_code=202)
        self.router.add_api_route("/nav/cancel", self.post_cancel, methods=["POST"])
        self.router.add_api_route("/nav/status", self.get_status, methods=["GET"])

    def attach(self, control_state):
        self.control = control_state

    # -- inputs from mapping and control --------------------------------------------

    def sync_generation(self):
        """Detect a new sensor session; returns the current generation."""
        identity = self.mapping.identity if self.mapping is not None else None  # replaced, never mutated
        with self.lock:
            if identity != self.identity:
                self.identity = identity
                self.generation += 1
                self.goal = self.cmd = self.frame = self.frame_at = None
                self.remote = dict(self.remote, state="idle", goal_id=None, path=[], frontiers=[])
            return self.generation

    def offer_frame(self, frame):
        """MapState frame observer: keep only the newest depth frame (no queue).

        Only normal-tracking frames of the current sensor session count; any other
        frame withdraws depth authority until a good one arrives.
        """
        self.sync_generation()
        header = frame.header
        good = (header.get("tracking") == "normal" and getattr(self.mapping, "tracking", None) == "normal"
                and (header.get("session_id"), header.get("map_epoch")) == self.identity)
        with self.lock:
            self.frame = frame if good else None
            self.frame_at = header.get("_received_monotonic", time.monotonic()) if good else None
            self.frame_seq += 1

    def sensor_fault(self):
        """None when measured navigation sensors are fresh, else a stop reason.

        A fresh RGB camera is enough to teleoperate, not to navigate: Nav2 motion
        also needs current-session normal ARKit tracking and LiDAR depth.
        """
        generation = self.sync_generation()
        transform, tracking, source, _ = self.pose_snapshot()
        if transform is None:
            return "nav_tracking_limited" if source == "limited" else "nav_pose_lost"
        with self.lock:
            if generation != self.generation:
                return "nav_sensor_reset"
            if self.frame is None or self.frame_at is None or time.monotonic() - self.frame_at >= self.DEPTH_TTL:
                return "nav_depth_lost"
        return None

    def sync(self, mode, target_id, active):
        """Called by ControlState on every output tick with the operator's current intent."""
        mode = mode if mode in NAV_MODES else None
        self.sync_generation()
        with self.lock:
            if mode in ("explore", "seek") and self.goal is not None:
                self.goal = None  # selecting another autonomy mode disarms go-to-point
            # A release keeps the goal armed; the next held "goto" heartbeat resumes it.
            active = bool(active and mode)
            if mode != "seek" or not active or (self.seek and self.seek["target_id"] != target_id):
                self.seek = None
            if active and not self.active:
                self.cmd = None  # never resume on a command computed before this press
            self.mode, self.target_id, self.active = mode, target_id, active

    def disarm(self):
        """Stick input, cancel, STOP or a controller change: forget the go-to-point goal."""
        with self.lock:
            self.goal = None
            self.cmd = None

    # -- backend state ----------------------------------------------------------------

    def available(self):
        with self.lock:
            return self.bridge is not None and self.clock() - self.bridge_seen < self.BRIDGE_TTL

    def handles(self, mode):
        return mode in self.backed_modes and self.available()

    def command(self, si=False):
        """Fresh Nav2 command for ControlState, or None when none exists.

        Normalized (v, w) with positive w turning right by default; with ``si``
        the raw cmd_vel (m/s, rad/s counter-clockwise) for a calibrated drive profile.
        """
        with self.lock:
            if not self.available() or self.mode not in self.backed_modes or not self.active:
                return None
            if self.mode == "goto" and (self.goal is None or self._remote_state() == "arrived"):
                return 0.0, 0.0
            if self.mode == "seek" and (self.seek is None or self.seek["state"] in ("arrived", "failed")
                                        or self.seek["goal"] is None):
                return 0.0, 0.0  # no located person yet, already at the standoff, or the target is lost
            if self.cmd is None or self.clock() - self.cmd[2] > self.CMD_TTL:
                return None
            return self.cmd[3:5] if si else self.cmd[0:2]

    def _remote_state(self):
        remote = self.remote
        if self.mode == "goto" and remote.get("goal_id") != self.goal_id:
            return "planning"
        state = remote.get("state")
        return state if state in STATES else "planning"

    def nav_object(self, mode=None, phase=None, active=None):
        """The viewer contract (docs/interfaces.md); mode/phase come from ControlState."""
        fault = self.sensor_fault() if self.available() else None
        with self.lock:
            mode = mode if mode in NAV_MODES else None
            active = self.active if active is None else active
            available = self.available()
            backend = "nav2" if available and (mode is None or mode in self.backed_modes) else "fallback"
            goal = path = None
            frontiers = []
            if backend == "nav2":
                if mode is None and self.goal is None:
                    state, message = "idle", "Nav2 ready"
                elif mode == "goto" and self.goal is None:
                    state, message = "idle", "No go-to-point goal; POST /nav/goal"
                elif mode is None and self.goal is not None:
                    state, message = "idle", "Go-to-point armed; hold the deadman to resume"
                elif not active:
                    state, message = "idle", "Hold the deadman to navigate"
                else:
                    state = self._remote_state()
                    message = str(self.remote.get("message") or "")[:200]
                    path = self._world_points(self.remote.get("path") or [], 200, 0.03)
                    seek = self.seek if mode == "seek" else None
                    if mode == "seek" and (seek is None or (seek["goal"] is None and seek["state"] == "planning")):
                        state, message, path = "planning", "Locating the person in LiDAR depth", None
                    elif seek and seek["state"] == "arrived":
                        state, message, path = "arrived", f"At the person, {seek['distance']:.1f} m away", None
                    elif seek and seek["state"] == "failed":
                        state, message, path = "failed", "Target lost", None
                    elif seek:
                        message = f"Following the person, {seek['distance']:.1f} m away"
                if fault:  # Nav2 is up but cannot be trusted to drive: say why
                    state, message, path = "unavailable", SENSOR_MESSAGES[fault], None
                if self.goal is not None:
                    goal = dict(x=round(self.goal[0], 3), z=round(self.goal[1], 3))
                if mode == "seek" and self.seek and self.seek["goal"] is not None:
                    goal = dict(x=round(self.seek["goal"][0], 3), z=round(self.seek["goal"][1], 3))
                frontiers = self._world_points(self.remote.get("frontiers") or [], 50, 0)
            elif mode in ("explore", "seek"):
                mapped = {"arrived": "arrived", "target_lost": "failed"}
                state = mapped.get(phase, "driving" if active else "idle")
                message = ("Nav2 bridge not connected; reactive fallback" if not available
                           else f"{mode} uses the reactive policy")
            else:
                state, backend = "unavailable", None  # nothing can drive this mode
                message = ("Nav2 bridge not connected" if self.bridge is None
                           else "Nav2 bridge silent; commands stopped")
                if mode == "goto" and self.goal is not None:
                    goal = dict(x=round(self.goal[0], 3), z=round(self.goal[1], 3))
            return dict(state=state, mode=mode, goal=goal, path=path or [], frontiers=frontiers,
                        backend=backend, message=message)

    def _world_points(self, odom_points, limit, tolerance):
        points = []
        for point in odom_points:
            if (isinstance(point, (list, tuple)) and len(point) >= 2
                    and all(isinstance(c, (int, float)) and math.isfinite(c) for c in point[:2])):
                x, z = odom_to_world(point[0], point[1])
                points.append((x, z))
        points = simplify(points, tolerance, limit) if tolerance else points[:limit]
        return [[round(x, 3) + 0.0, round(z, 3) + 0.0] for x, z in points]  # no "-0.0" in JSON

    def detail(self):
        pose_source = self.pose_snapshot()[2] if self.mapping is not None else None  # never under self.lock
        fault = self.sensor_fault() if self.mapping is not None else "nav_pose_lost"
        with self.lock:
            return dict(available=self.available(), bridge=self.bridge is not None,
                        bridge_age_ms=None if self.bridge is None else round((self.clock() - self.bridge_seen) * 1000),
                        bridge_hello=self.bridge_hello, goal_id=self.goal_id, backed_modes=sorted(self.backed_modes),
                        remote={key: self.remote.get(key) for key in ("state", "goal_id", "message", "controller_hz",
                                                                      "planner_ms", "recoveries", "result", "costmap",
                                                                      "nav2")},
                        pose_source=pose_source, sensor_fault=fault, generation=self.generation,
                        depth_age_ms=None if self.frame_at is None else round((time.monotonic() - self.frame_at) * 1000),
                        speed_mps=self.speed_mps, turn_radps=self.turn_radps, max_v=self.max_v, max_w=self.max_w)

    # -- pose and sensor streams for the bridge ---------------------------------------

    def pose_snapshot(self):
        """(camera-to-world transform, tracking, source, age_s); transform is None unless the
        current sensor session has a fresh pose with normal ARKit tracking (source then says why:
        "limited" or "lost"). Nothing else is navigation authority: no dead reckoning."""
        mapping = self.mapping
        # Lock-free read: MapState replaces (never mutates) these, and fusion holds
        # mapping.lock for whole voxel integrations that pose streaming must not wait on.
        transform, tracking = mapping.pose.copy(), mapping.tracking
        received, connected = mapping.last_received, mapping.connected
        scan, demo, identity = mapping.scan, mapping.demo, mapping.identity
        age = time.monotonic() - received if received else None
        if not connected or age is None or age >= self.POSE_TTL or scan or identity is None:
            return None, tracking, "lost", age
        if tracking != "normal":
            return None, tracking, "limited", age
        return transform, tracking, "simulated" if demo else "arkit", age

    def hello(self):
        """Server hello: drive scaling and, with a drive profile, the chassis calibration."""
        message = dict(type="hello", version=PROTOCOL, speed_mps=self.speed_mps, turn_radps=self.turn_radps,
                       max_v=self.max_v, max_w=self.max_w, camera_height=self.camera_height, frame="arkit",
                       camera_yaw_offset_rad=0.0, drive_profile=None, generation=self.generation)
        profile = getattr(self.control, "profile", None)
        if profile is not None:
            max_pwm = self.control.max_pwm
            message.update(camera_yaw_offset_rad=profile.camera_yaw_offset_rad, drive_profile=dict(
                max_linear_mps=round(profile.max_linear_mps(max_pwm), 3),
                max_angular_radps=round(profile.max_angular_radps(max_pwm), 3),
                half_track_m=profile.half_track_m))
        return message

    def intent(self):
        with self.lock:
            goal, goal_id = None, self.goal_id
            if self.mode == "goto" and self.goal is not None:
                X, Y = world_to_odom(*self.goal)
                goal = dict(x=X, y=Y, yaw=None)
            elif self.mode == "seek" and self.seek and self.seek["goal"] is not None and self.seek["state"] == "driving":
                X, Y = world_to_odom(*self.seek["goal"])
                goal, goal_id = dict(x=X, y=Y, yaw=None), self.seek["goal_id"]
            return dict(type="intent", generation=self.generation, active=self.active and self.mode in self.backed_modes,
                        mode=self.mode if self.mode in self.backed_modes else None,
                        goal=goal, goal_id=goal_id, target_id=self.target_id)

    # -- Seek ---------------------------------------------------------------------------

    SEEK_LOST_S = 3.0      # target unseen this long: Seek fails and stops
    SEEK_MOVE_M = 0.3      # re-send the standoff goal when the person moved this far

    def locate(self, box, frame):
        """Floor (x, z) of a detected person from the LiDAR depth inside their box, or None."""
        depth = np.asarray(frame.depth, np.float32)
        height, width = depth.shape
        u0, u1 = int((box["x"] + 0.3 * box["w"]) * width), int(math.ceil((box["x"] + 0.7 * box["w"]) * width))
        v0, v1 = int((box["y"] + 0.25 * box["h"]) * height), int(math.ceil((box["y"] + 0.6 * box["h"]) * height))
        patch = depth[max(0, v0):min(height, v1), max(0, u0):min(width, u1)]
        confidence = getattr(frame, "confidence", None)
        valid = np.isfinite(patch) & (patch > 0.15) & (patch <= 8)
        if confidence is not None:
            valid &= np.asarray(confidence)[max(0, v0):min(height, v1), max(0, u0):min(width, u1)] >= 1
        if valid.sum() < 3:
            return None
        d = float(np.median(patch[valid]))
        k = frame.calibration
        u, v = (u0 + u1) / 2, (v0 + v1) / 2
        point = np.asarray(frame.transform, float) @ np.array([(u - k.cx) / k.fx * d, -(v - k.cy) / k.fy * d, -d, 1.0])
        return float(point[0]), float(point[2])

    def update_seek(self, detections):
        """ControlState calls this each tick in a Nav2-backed Seek with its detection snapshot."""
        now = time.monotonic()
        with self.lock:
            if self.mode != "seek" or not self.active or now - self.seek_checked < 0.2:
                return
            self.seek_checked = now
            frame, target_id = self.frame, self.target_id
            seek = self.seek or dict(target_id=target_id, person=None, seen_at=now, goal=None, goal_id=None,
                                     state="planning", distance=None)
        boxes = (detections.get("boxes", []) if detections and detections.get("fresh")
                 and not detections.get("error") else [])
        box = next((b for b in boxes if b.get("id") == target_id and b.get("cls") == "person"), None)
        person = self.locate(box, frame) if box is not None and frame is not None else None
        transform = self.pose_snapshot()[0]
        if person is not None and transform is not None:
            rover = (float(transform[0, 3]), float(transform[2, 3]))
            dx, dz = person[0] - rover[0], person[1] - rover[1]
            distance = math.hypot(dx, dz)
            seek.update(person=person, seen_at=now, distance=distance)
            if distance <= self.standoff + 0.15:
                seek["state"] = "arrived"
            else:
                goal = (person[0] - self.standoff * dx / distance, person[1] - self.standoff * dz / distance)
                if seek["goal"] is None or seek["state"] != "driving" or math.dist(goal, seek["goal"]) > self.SEEK_MOVE_M:
                    with self.lock:
                        self.seek_seq += 1
                        seek["goal_id"] = f"seek-{self.seek_seq}"
                    seek["goal"] = goal
                seek["state"] = "driving"
        elif now - seek["seen_at"] >= self.SEEK_LOST_S:
            seek["state"] = "failed"
        with self.lock:
            if self.mode == "seek" and self.active and self.target_id == seek["target_id"]:
                self.seek = seek

    def receive(self, message):
        """Apply one bridge message (cmd_vel or status)."""
        if not isinstance(message, dict):
            raise ValueError("Bridge message must be an object")
        kind = message.get("type")
        with self.lock:
            now = self.clock()
            if kind == "cmd_vel":
                linear, angular = message.get("linear"), message.get("angular")
                if not all(type(x) in (int, float) and math.isfinite(x) for x in (linear, angular)):
                    raise ValueError("cmd_vel needs finite linear and angular")
                if self.mode == "goto" and message.get("goal_id", self.goal_id) != self.goal_id:
                    self.bridge_seen = now
                    return  # computed for an earlier goal
                # ROS angular.z is counter-clockwise (left); ControlState positive w turns right.
                v = max(-self.max_v, min(self.max_v, linear / self.speed_mps))
                w = max(-self.max_w, min(self.max_w, -angular / self.turn_radps))
                self.cmd = (v, w, now, float(linear), float(angular))
            elif kind == "status":
                self.remote = {key: message.get(key) for key in
                               ("state", "goal_id", "path", "frontiers", "message", "controller_hz",
                                "planner_ms", "recoveries", "result", "costmap", "nav2")}
            elif kind != "pong":
                raise ValueError("Unknown bridge message type")
            self.bridge_seen = now

    async def websocket(self, ws: WebSocket):
        await ws.accept()
        host = ws.client.host if ws.client else None
        if host not in LOOPBACK:
            await ws.close(code=1008, reason="The navigation bridge must run on this computer")
            return
        token = object()
        with self.lock:
            busy = self.bridge is not None
            if not busy:
                self.bridge = token
        if busy:  # never await while holding the threading lock
            await ws.close(code=1008, reason="Another navigation bridge is connected")
            return
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 10)
            if not isinstance(hello, dict) or hello.get("type") != "hello" or hello.get("version") != PROTOCOL:
                await ws.close(code=1008, reason="Expected bridge hello version 1")
                return
            with self.lock:
                self.bridge_hello = {k: hello.get(k) for k in ("node", "nav2", "controller", "slam", "costmap_source")}
                self.bridge_seen = self.clock()
            await ws.send_json(self.hello())

            async def send():
                last_pose = last_range = last_intent_at = 0.0
                sent_frame, sent_intent, sent_generation = -1, None, self.generation
                while True:
                    now = time.monotonic()
                    generation = self.sync_generation()
                    if generation != sent_generation:
                        # New sensor session: the bridge cancels its goal, clears costmaps and
                        # odometry history, and drops samples tagged with an older generation.
                        await ws.send_json(dict(type="reset", generation=generation, reason="sensor session changed"))
                        sent_generation = generation
                    if now - last_pose >= 0.04:
                        transform, tracking, source, age = await asyncio.to_thread(self.pose_snapshot)
                        if transform is not None:  # limited/lost tracking never renews TF or odometry
                            await ws.send_json(dict(type="pose", transform=transform.flatten(order="F").tolist(),
                                                    tracking=tracking, source=source, generation=generation,
                                                    age_ms=round((age or 0) * 1000, 1),
                                                    floor_y=-self.camera_height))
                        last_pose = now
                    with self.lock:  # a generation change clears self.frame under this lock
                        frame, seq, frame_generation = self.frame, self.frame_seq, self.generation
                    if frame is not None and seq != sent_frame:
                        received = frame.header.get("_received_monotonic", now)
                        source = "simulated" if self.mapping.demo else "arkit"
                        packet = await asyncio.to_thread(encode_depth, frame, floor_y=-self.camera_height, source=source,
                                                         age_ms=round(max(0, now - received) * 1000, 1),
                                                         generation=frame_generation)
                        await ws.send_bytes(packet)
                    sent_frame = seq
                    if self.control is not None and now - last_range >= 0.1:
                        cm, at = self.control.range_cm, self.control.range_at  # no control.lock on the event loop
                        await ws.send_json(dict(type="range", cm=cm,
                                                age_ms=None if not math.isfinite(at) else round((self.control.clock() - at) * 1000)))
                        last_range = now
                    intent = self.intent()
                    if intent != sent_intent or now - last_intent_at >= 0.2:
                        await ws.send_json(intent)
                        sent_intent, last_intent_at = intent, now
                    await asyncio.sleep(0.02)

            async def receive():
                while True:
                    envelope = await ws.receive()
                    if envelope["type"] == "websocket.disconnect":
                        return
                    if envelope.get("text") is None or len(envelope["text"]) > 262144:
                        raise ValueError("Bridge messages are bounded JSON text")
                    self.receive(json.loads(envelope["text"]))

            tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
        except (WebSocketDisconnect, RuntimeError, OSError, asyncio.TimeoutError, ValueError, TypeError):
            with suppress(RuntimeError, OSError, WebSocketDisconnect):
                await ws.close(code=1008)
        finally:
            with self.lock:
                if self.bridge is token:
                    self.bridge, self.cmd, self.remote = None, None, {}

    # -- HTTP --------------------------------------------------------------------------

    def status_object(self):
        if self.control is None:
            return self.nav_object()
        return self.control.status()["nav"]

    def set_goal(self, x, z, session_id=None, permit=None):
        if not (math.isfinite(x) and math.isfinite(z)) or max(abs(x), abs(z)) > 500:
            raise HTTPException(422, "Goal must be finite voxel-map metres")
        state = self.control
        if state is None:
            raise Refused(self.nav_object(), "No rover control on this server")
        if not self.available():
            raise Refused(self.status_object(), "Navigation backend unavailable (Nav2 bridge not running)")
        fault = self.sensor_fault()
        if fault:
            raise Refused(self.status_object(), SENSOR_MESSAGES[fault])
        with state.lock:
            now = state.clock()
            state.output()  # apply expiry before judging the lease
            live = (state.session_id and state.connected and not state.requires_release
                    and state.deadline > now and state.camera_is_ready())
            if permit is not None or session_id is not None:
                live = live and session_id == state.session_id and state.permits.get(permit, 0) > now
            if not live or state.autonomy is None:
                raise Refused(state.status()["nav"], "Go-to-point needs a live operator permit: hold the deadman")
            if state.scan_token is not None:
                raise Refused(state.status()["nav"], "A scan is running")
            # Arm only: the operator's own held "goto" heartbeats start motion. A POST
            # never changes the drive mode, so no other client can make the rover move.
            with self.lock:
                self.goal_id += 1
                self.goal = (float(x), float(z))
                self.cmd = None
                self.remote = dict(self.remote, state="planning", path=[])
            state.stick_override = False
            return self.nav_object("goto", "goto", True)

    def cancel(self):
        state = self.control
        if state is not None:
            with state.lock:
                if state.autonomy is not None and state.autonomy.mode in NAV_MODES:
                    state.autonomy.reset()
                    state.stick_override = False
                self.disarm()
                state.output()
                return state.status()["nav"]
        self.disarm()
        return self.nav_object()

    async def post_goal(self, body: GoalRequest):
        try:
            return await asyncio.to_thread(self.set_goal, body.x, body.z, body.session_id, body.permit)
        except Refused as refused:
            return JSONResponse(dict(nav=refused.nav, message=refused.message), status_code=409)

    async def post_cancel(self):
        return await asyncio.to_thread(self.cancel)

    async def get_status(self):
        def build():
            return dict(self.status_object(), detail=self.detail())
        return await asyncio.to_thread(build)


def disabled_router():
    """Routes for a server without --nav2: the contract answers, but nothing navigates."""
    router = APIRouter()
    message = "Navigation disabled: start jetson.server with --nav2 (FARSIGHT_NAV2=1)"

    async def goal(body: GoalRequest):
        return JSONResponse(dict(nav=empty_nav("unavailable", message), message=message), status_code=409)

    async def cancel():
        return empty_nav("idle", "")

    async def status():
        return dict(empty_nav("unavailable", message), detail=dict(enabled=False))

    async def bridge(ws: WebSocket):
        await ws.accept()
        await ws.close(code=1008, reason=message[:100])

    router.add_api_route("/nav/goal", goal, methods=["POST"], status_code=202)
    router.add_api_route("/nav/cancel", cancel, methods=["POST"])
    router.add_api_route("/nav/status", status, methods=["GET"])
    router.add_api_websocket_route("/ws/nav", bridge)
    return router
