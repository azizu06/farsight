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


def encode_depth(frame, *, floor_y, source, age_ms, max_width=160, max_range=6.0):
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
                  floor_y=floor_y, source=source, age_ms=age_ms)
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
    POSE_TTL = 0.5

    def __init__(self, mapping, *, speed_mps=0.45, turn_radps=3.0, max_v=0.7, max_w=0.8,
                 camera_height=0.15, backed_modes=("goto",), clock=time.monotonic):
        for name, value in (("speed_mps", speed_mps), ("turn_radps", turn_radps), ("max_v", max_v),
                            ("max_w", max_w), ("camera_height", camera_height)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Navigation {name} must be positive")
        self.mapping, self.clock = mapping, clock
        self.speed_mps, self.turn_radps = speed_mps, turn_radps
        self.max_v, self.max_w = min(max_v, 1.0), min(max_w, 1.0)
        self.camera_height = camera_height
        self.backed_modes = frozenset(backed_modes)
        self.control = None
        self.lock = threading.RLock()
        self.bridge = None
        self.bridge_seen = -math.inf
        self.bridge_hello = None
        self.cmd = None             # (v, w normalized, received monotonic)
        self.remote = {}            # latest bridge status
        self.mode, self.target_id, self.active = None, None, False
        self.goal = None            # voxel-map (x, z)
        self.goal_id = 0
        self.frame = None           # newest RGB-D frame offered by mapping or the simulator
        self.frame_seq = 0
        self.dr = [0.0, 0.0, 0.0]   # dead-reckoned x, z, yaw when the phone gives no pose
        self.dr_last = None
        self.dr_v = self.dr_w = 0.0
        self.router = APIRouter()
        self.router.add_api_websocket_route("/ws/nav", self.websocket)
        self.router.add_api_route("/nav/goal", self.post_goal, methods=["POST"], status_code=202)
        self.router.add_api_route("/nav/cancel", self.post_cancel, methods=["POST"])
        self.router.add_api_route("/nav/status", self.get_status, methods=["GET"])

    def attach(self, control_state):
        self.control = control_state

    # -- inputs from mapping and control --------------------------------------------

    def offer_frame(self, frame):
        """MapState frame observer: keep only the newest depth frame (no queue)."""
        with self.lock:
            self.frame = frame
            self.frame_seq += 1

    def on_output(self, v, w, blocked=False):
        """ControlState.on_output: integrate the applied command for the dead-reckoning pose."""
        with self.lock:
            now = self.clock()
            if self.dr_last is not None:
                dt = min(max(now - self.dr_last, 0), 0.25)
                yaw = self.dr[2] - self.dr_w * self.turn_radps * dt
                mid = (self.dr[2] + yaw) / 2
                self.dr[0] -= math.sin(mid) * self.dr_v * self.speed_mps * dt
                self.dr[1] -= math.cos(mid) * self.dr_v * self.speed_mps * dt
                self.dr[2] = math.atan2(math.sin(yaw), math.cos(yaw))
            self.dr_last, self.dr_v, self.dr_w = now, float(v), float(w)

    def sync(self, mode, target_id, active):
        """Called by ControlState on every output tick with the operator's current intent."""
        mode = mode if mode in NAV_MODES else None
        with self.lock:
            if mode != "goto" and self.goal is not None:
                self.goal = None  # go-to-point ends with release, override, cancel or another mode
            self.mode, self.target_id, self.active = mode, target_id, bool(active and mode)

    # -- backend state ----------------------------------------------------------------

    def available(self):
        with self.lock:
            return self.bridge is not None and self.clock() - self.bridge_seen < self.BRIDGE_TTL

    def handles(self, mode):
        return mode in self.backed_modes and self.available()

    def command(self):
        """Normalized (v, w) for ControlState, or None when no fresh command exists."""
        with self.lock:
            if not self.available() or self.mode not in self.backed_modes or not self.active:
                return None
            if self.mode == "goto" and (self.goal is None or self._remote_state() == "arrived"):
                return 0.0, 0.0
            if self.cmd is None or self.clock() - self.cmd[2] > self.CMD_TTL:
                return None
            return self.cmd[0], self.cmd[1]

    def _remote_state(self):
        remote = self.remote
        if self.mode == "goto" and remote.get("goal_id") != self.goal_id:
            return "planning"
        state = remote.get("state")
        return state if state in STATES else "planning"

    def nav_object(self, mode=None, phase=None, active=None):
        """The viewer contract (docs/interfaces.md); mode/phase come from ControlState."""
        with self.lock:
            mode = mode if mode in NAV_MODES else None
            active = self.active if active is None else active
            available = self.available()
            backend = "nav2" if available and (mode is None or mode in self.backed_modes) else "fallback"
            goal = path = None
            frontiers = []
            if backend == "nav2":
                if mode is None:
                    state, message = "idle", "Nav2 ready"
                elif mode == "goto" and self.goal is None:
                    state, message = "idle", "No go-to-point goal; POST /nav/goal"
                elif not active:
                    state, message = "idle", "Hold the deadman to navigate"
                else:
                    state = self._remote_state()
                    message = str(self.remote.get("message") or "")[:200]
                    path = self._world_points(self.remote.get("path") or [], 200, 0.03)
                if mode == "goto" and self.goal is not None:
                    goal = dict(x=round(self.goal[0], 3), z=round(self.goal[1], 3))
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
        with self.lock:
            return dict(available=self.available(), bridge=self.bridge is not None,
                        bridge_age_ms=None if self.bridge is None else round((self.clock() - self.bridge_seen) * 1000),
                        bridge_hello=self.bridge_hello, goal_id=self.goal_id, backed_modes=sorted(self.backed_modes),
                        remote={key: self.remote.get(key) for key in ("state", "goal_id", "message", "controller_hz",
                                                                      "planner_ms", "recoveries", "result", "costmap",
                                                                      "nav2")},
                        pose_source=self.pose_snapshot()[2] if self.mapping is not None else None,
                        speed_mps=self.speed_mps, turn_radps=self.turn_radps, max_v=self.max_v, max_w=self.max_w)

    # -- pose and sensor streams for the bridge ---------------------------------------

    def pose_snapshot(self):
        """(camera-to-world transform, tracking, source, age_s) or (None, ...) when the pose is lost."""
        mapping = self.mapping
        with mapping.lock:
            transform, tracking = mapping.pose.copy(), mapping.tracking
            received, connected = mapping.last_received, mapping.connected
            scan, demo, identity = mapping.scan, mapping.demo, mapping.identity
        now = time.monotonic()
        age = now - received if received else None
        if connected and age is not None and age < self.POSE_TTL and not scan:
            return transform, tracking, "simulated" if demo else "arkit", age
        if identity is None:
            with self.lock:
                x, z, yaw = self.dr
            c, s = math.cos(yaw), math.sin(yaw)
            pose = np.eye(4)
            pose[:3, :3] = [[c, 0, s], [0, 1, 0], [-s, 0, c]]
            pose[:3, 3] = [x, 0, z]
            return pose, "normal", "dead_reckoning", 0.0
        return None, tracking, "lost", age

    def intent(self):
        with self.lock:
            goal = None
            if self.mode == "goto" and self.goal is not None:
                X, Y = world_to_odom(*self.goal)
                goal = dict(x=X, y=Y, yaw=None)
            return dict(type="intent", active=self.active and self.mode in self.backed_modes,
                        mode=self.mode if self.mode in self.backed_modes else None,
                        goal=goal, goal_id=self.goal_id, target_id=self.target_id)

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
                # ROS angular.z is counter-clockwise (left); ControlState positive w turns right.
                v = max(-self.max_v, min(self.max_v, linear / self.speed_mps))
                w = max(-self.max_w, min(self.max_w, -angular / self.turn_radps))
                self.cmd = (v, w, now)
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
            if self.bridge is not None:
                await ws.close(code=1008, reason="Another navigation bridge is connected")
                return
            self.bridge = token
        try:
            hello = await asyncio.wait_for(ws.receive_json(), 10)
            if not isinstance(hello, dict) or hello.get("type") != "hello" or hello.get("version") != PROTOCOL:
                await ws.close(code=1008, reason="Expected bridge hello version 1")
                return
            with self.lock:
                self.bridge_hello = {k: hello.get(k) for k in ("node", "nav2", "controller", "slam", "costmap_source")}
                self.bridge_seen = self.clock()
            await ws.send_json(dict(type="hello", version=PROTOCOL, speed_mps=self.speed_mps,
                                    turn_radps=self.turn_radps, max_v=self.max_v, max_w=self.max_w,
                                    camera_height=self.camera_height, frame="arkit"))

            async def send():
                last_pose = last_range = last_intent_at = 0.0
                sent_frame, sent_intent = -1, None
                while True:
                    now = time.monotonic()
                    if now - last_pose >= 0.04:
                        transform, tracking, source, age = await asyncio.to_thread(self.pose_snapshot)
                        if transform is not None:
                            await ws.send_json(dict(type="pose", transform=transform.flatten(order="F").tolist(),
                                                    tracking=tracking, source=source,
                                                    age_ms=round((age or 0) * 1000, 1),
                                                    floor_y=-self.camera_height))
                        last_pose = now
                    with self.lock:
                        frame, seq = self.frame, self.frame_seq
                    if frame is not None and seq != sent_frame:
                        received = frame.header.get("_received_monotonic", now)
                        source = "simulated" if self.mapping.demo else "arkit"
                        packet = await asyncio.to_thread(encode_depth, frame, floor_y=-self.camera_height, source=source,
                                                         age_ms=round(max(0, now - received) * 1000, 1))
                        await ws.send_bytes(packet)
                        sent_frame = seq
                    if self.control is not None and now - last_range >= 0.1:
                        with self.control.lock:
                            cm, at = self.control.range_cm, self.control.range_at
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
            with self.lock:
                self.goal_id += 1
                self.goal = (float(x), float(z))
                self.remote = dict(self.remote, state="planning", path=[])
            state.stick_override = False
            state.goto_race_permits = frozenset(state.permits)
            state.autonomy.select("goto", None, now)
            with self.lock:
                self.mode, self.target_id, self.active = "goto", None, True
                self.goal = (float(x), float(z))  # select may have synced a previous mode
            return state.status()["nav"]

    def cancel(self):
        state = self.control
        if state is not None:
            with state.lock:
                if state.autonomy is not None and state.autonomy.mode in NAV_MODES:
                    state.autonomy.reset()
                    state.stick_override = False
                with self.lock:
                    self.goal = None
                state.output()
                return state.status()["nav"]
        with self.lock:
            self.goal = None
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
