"""Control policy independent of sockets and serial hardware.

Permits are issued against server monotonic time: a delayed command can never
renew a lease beyond the expiry of the permit it carries.
"""

from collections import OrderedDict
import math
import secrets
import threading
import time
import uuid

from jetson.autonomy import Autonomy


class ControlState:
    LEASE = 0.250
    RANGE_TTL = 0.350

    def __init__(self, simulated=False, max_pwm=100, clock=time.monotonic, camera_ready=None, min_pwm=0,
                 detections=None):
        if (type(min_pwm) is not int or type(max_pwm) is not int
                or not 0 <= min_pwm < max_pwm <= 255):
            raise ValueError("PWM bounds must satisfy 0 <= min_pwm < max_pwm <= 255")
        self.clock, self.simulated, self.max_pwm = clock, simulated, max_pwm
        self.min_pwm = min_pwm
        self.camera_ready = camera_ready
        # Optional, bounded in-memory snapshot reader; never inference or I/O.
        self.detections = detections
        self.autonomy = Autonomy() if detections is not None else None
        self.stick_override = False
        self.lock = threading.RLock()
        self.session_id = None
        self.permits = OrderedDict()
        self.seq = -1
        self.connected = False
        self.range_cm = None
        self.range_at = -math.inf
        self.pan = 0
        self.v = self.w = 0.0
        self.deadline = 0.0
        self.requires_release = True
        self.reason = "rover_disabled"
        self.link_ms = None
        self.scan_token = None
        self.scan_w = 0.0
        # Permits already issued when the scan began: a zero-stick teleop
        # heartbeat carrying one was sent before POST /scan and may race it.
        self.scan_race_permits = frozenset()
        # Optional on_output(v, w, forward_blocked) observer of the command actually applied.
        self.on_output = None

    def begin_scan(self, session_id, permit):
        """Reserve stopped control; never issue/renew an operator lease here."""
        with self.lock:
            left, right, _ = self.output()
            if (self.scan_token is not None or not self.session_id or session_id != self.session_id
                    or not isinstance(permit, str) or self.permits.get(permit, 0) <= self.clock()
                    or not self.connected or self.requires_release or self.deadline <= self.clock()
                    or not self.camera_is_ready() or left or right or self.v or self.w
                    or (self.autonomy and self.autonomy.mode != "teleop")):
                raise ValueError("Scan requires a stopped rover and a live operator-held permit")
            self.scan_token = object()
            self.scan_w = 0.0
            self.scan_race_permits = frozenset(self.permits)
            return self.scan_token

    def scan_command(self, token, *, w=0, pan=0):
        with self.lock:
            self.output()  # enforce expiry, camera and range gates
            if token is None or token is not self.scan_token:
                raise ValueError("Scan cancelled: operator lease, override, or safety stop")
            if not math.isfinite(w) or not -.35 <= w <= .35 or not -80 <= pan <= 80:
                raise ValueError("Scan command outside bounded sweep")
            self.scan_w, self.pan = w, round(pan)
            self.output()
            if token is not self.scan_token:
                raise ValueError("Scan cancelled: camera or range unavailable")

    def end_scan(self, token):
        with self.lock:
            if token is self.scan_token:
                self.stop("scan_finished")

    def stop(self, reason, require_release=True):
        with self.lock:
            self.v = self.w = 0.0
            self.deadline = 0.0
            self.scan_token = None
            self.scan_w = 0.0
            self.scan_race_permits = frozenset()
            self.reason = reason
            if self.autonomy:
                self.autonomy.reset()
            self.stick_override = False
            if require_release:
                self.requires_release = True

    def set_connected(self, value, reason=None):
        with self.lock:
            if value != self.connected:
                self.stop("release_required" if value else (reason or "serial_disconnected"))
                self.permits.clear()
                self.range_cm = None
                self.range_at = -math.inf
            self.connected = value
            if reason:
                self.reason = reason

    def begin(self):
        with self.lock:
            if self.session_id is not None:
                return None
            self.session_id = str(uuid.uuid4())
            self.seq = -1
            self.permits.clear()
            self.stop("release_required")
            return self.session_id

    def end(self, session_id):
        with self.lock:
            if self.session_id == session_id:
                self.stop("controller_disconnected")
                self.session_id = None
                self.permits.clear()

    def update_range(self, cm):
        with self.lock:
            self.range_cm = cm if math.isfinite(cm) and 0 < cm <= 500 else None
            self.range_at = self.clock()

    def forward_blocked(self):
        return (self.range_cm is None or self.clock() - self.range_at >= self.RANGE_TTL
                or self.range_cm < 25 or abs(self.pan) > 10)

    def camera_is_ready(self):
        if self.camera_ready is None:
            return True
        try:
            return bool(self.camera_ready())
        except Exception:
            return False

    def accept(self, message, session_id):
        with self.lock:
            if not isinstance(message, dict) or self.session_id != session_id:
                return False
            if message.get("session_id") != session_id:
                self.stop("wrong_session")
                return False
            seq = message.get("seq")
            if type(seq) is not int or not 0 <= seq <= 2**53 - 1:
                self.stop("invalid_sequence")
                return False
            # Any release stops, including stale releases, but only an ordered
            # release can unlock the next press.
            if message.get("deadman") is False:
                self.stop("deadman_released")
                if seq > self.seq:
                    self.seq = seq
                    self.requires_release = False
                return True
            if seq <= self.seq or message.get("deadman") is not True:
                self.stop("stale_sequence")
                return False
            token = message.get("permit")
            expires = self.permits.get(token, 0) if isinstance(token, str) else 0
            now = self.clock()
            if expires <= now:
                self.stop("expired_permit")
                return False
            if not self.connected or self.requires_release:
                self.stop("release_required" if self.connected else "serial_disconnected")
                return False
            if not self.camera_is_ready():
                self.stop("camera_stale")
                return False
            mode = message.get("mode", "teleop")
            if mode == "scan":
                if self.scan_token is None:
                    self.stop("scan_not_active")
                    return False
            elif mode != "teleop" and (not self.autonomy or mode not in ("explore", "seek")):
                self.stop("unsupported_mode")
                return False
            target_id = message.get("target_id") if mode == "seek" else None
            if mode == "seek" and (type(target_id) is not int or not 0 <= target_id <= 2**53 - 1):
                self.stop("invalid_target")
                return False
            video_age = message.get("video_age_ms")
            if (message.get("video_fresh") is not True or type(video_age) not in (int, float)
                    or not math.isfinite(video_age) or not 0 <= video_age <= 500):
                self.stop("video_stale")
                return False
            values = [message.get("v"), message.get("w"), message.get("pan", 0)]
            if any(type(x) not in (int, float) or not math.isfinite(x) for x in values):
                self.stop("invalid_command")
                return False
            v, w, pan = values
            if not (-1 <= v <= 1 and -1 <= w <= 1 and -80 <= pan <= 80):
                self.stop("invalid_command")
                return False
            if (self.scan_token is not None and mode == "teleop" and not v and not w
                    and token in self.scan_race_permits):
                mode = "scan"  # pre-POST heartbeat: renews like a scan heartbeat, within its own permit
            elif mode == "scan":
                self.scan_race_permits = frozenset()  # client is in scan mode; later teleop overrides
            if self.scan_token is not None:
                if mode != "scan" or v or w:
                    self.stop("scan_override", require_release=False)
                    mode = "teleop"
                else:
                    pan = self.pan
            if self.autonomy:
                if mode in ("teleop", "scan"):
                    self.autonomy.reset()
                    self.stick_override = False
                elif v or w or self.stick_override:
                    self.autonomy.reset()
                    self.stick_override = True
                else:
                    self.autonomy.select(mode, target_id, now)
                    pan = 0  # autonomy needs a forward-facing ultrasonic/camera
            if round(pan) != self.pan:
                # A reading from a previous servo direction is not a forward
                # obstacle reading, even when the servo returns to the centre.
                self.range_cm = None
                self.range_at = -math.inf
            self.seq, self.v, self.w, self.pan = seq, float(v), float(w), round(pan)
            self.deadline = min(now + self.LEASE, expires)
            self.link_ms = round((self.LEASE - (expires - now)) * 1000, 1)
            self.reason = "active"
            return True

    def output(self):
        with self.lock:
            if self.deadline and self.clock() >= self.deadline:
                self.stop("lease_expired")
            autonomous = self.autonomy is not None and self.autonomy.mode != "teleop"
            if (self.v or self.w or autonomous or self.scan_token is not None) and not self.camera_is_ready():
                self.stop("camera_stale")
            v, w = self.v, self.w
            if self.scan_token is not None:
                v, w = 0.0, self.scan_w
                if w and (self.range_cm is None or self.clock() - self.range_at >= self.RANGE_TTL
                          or self.range_cm < 25):
                    self.stop("scan_range_stop")
                    v = w = 0.0
            if self.autonomy and self.autonomy.mode != "teleop" and self.deadline:
                cm = self.range_cm if self.clock() - self.range_at < self.RANGE_TTL else None
                try:
                    v, w = self.autonomy.step(self.clock(), cm, self.detections())
                except Exception:
                    self.stop("autonomy_unavailable")
                    v = w = 0
            if not self.connected:
                v = w = 0
            blocked = v > 0 and self.forward_blocked()
            if blocked:
                v = 0
                self.reason = "forward_blocked"
            left, right = v + w, v - w  # positive w turns right
            scale = max(1.0, abs(left), abs(right))
            if self.on_output:
                try:
                    self.on_output(v / scale, w / scale, blocked)
                except Exception:
                    pass  # telemetry must never affect motor output
            def pwm(value):
                if value == 0:
                    return 0
                magnitude = round(self.min_pwm + (self.max_pwm - self.min_pwm) * abs(value / scale))
                return magnitude if value > 0 else -magnitude
            return pwm(left), pwm(right), self.pan + 90

    def status(self, issue_permit=False):
        with self.lock:
            left, right, pan = self.output()
            now = self.clock()
            for token, expiry in list(self.permits.items()):
                if expiry <= now:
                    del self.permits[token]
            permit = None
            if issue_permit and self.session_id:
                permit = secrets.token_urlsafe(18)
                self.permits[permit] = now + self.LEASE
            return {"type": "control_status", "session_id": self.session_id,
                    "permit": permit, "permit_ms": 250, "seq": self.seq,
                    "range_cm": self.range_cm, "estop": self.forward_blocked(),
                    "mode": "scan" if self.scan_token is not None else self.autonomy.mode if self.autonomy else "teleop", "link_ms": self.link_ms,
                    "autonomy_state": ("stick_override" if self.stick_override else
                                       self.autonomy.phase if self.autonomy else "idle"),
                    "target_id": self.autonomy.target_id if self.autonomy else None,
                    "rover": self.connected, "simulated": self.simulated,
                    "requires_release": self.requires_release, "reason": self.reason,
                    "left_pwm": left, "right_pwm": right, "pan_deg": pan,
                    "max_pwm": self.max_pwm, "min_pwm": self.min_pwm}
