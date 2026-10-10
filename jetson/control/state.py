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

from jetson.autonomy import Autonomy, valid_person


class ControlState:
    LEASE = 0.250
    RANGE_TTL = 0.350
    PAN_MOUNTS = ("servo", "none")

    def __init__(self, simulated=False, max_pwm=100, clock=time.monotonic, camera_ready=None, min_pwm=0,
                 detections=None, proximity_stop=True, navigator=None, drive_profile=None,
                 allow_calibration=False, pan_mount="servo", autonomy_max_pwm=None):
        if (type(min_pwm) is not int or type(max_pwm) is not int
                or not 0 <= min_pwm < max_pwm <= 255):
            raise ValueError("PWM bounds must satisfy 0 <= min_pwm < max_pwm <= 255")
        autonomy_max_pwm = max_pwm if autonomy_max_pwm is None else autonomy_max_pwm
        if type(autonomy_max_pwm) is not int or not min_pwm < autonomy_max_pwm <= max_pwm:
            raise ValueError("autonomy_max_pwm must satisfy min_pwm < autonomy_max_pwm <= max_pwm")
        self.clock, self.simulated, self.max_pwm = clock, simulated, max_pwm
        self.min_pwm = min_pwm
        # Ceiling for motion the operator does not steer directly (explore, seek,
        # goto, Nav2, scan sweeps), so a fast teleop max_pwm does not also speed
        # up autonomy. Defaults to max_pwm (unchanged behaviour).
        self.autonomy_max_pwm = autonomy_max_pwm
        self.camera_ready = camera_ready
        # Optional, bounded in-memory snapshot reader; never inference or I/O.
        self.detections = detections
        # Ultrasonic proximity stop (range < 25 cm or stale range blocks forward
        # translation). On by default; turning it off is an explicit opt-in
        # (server --proximity-stop off) for runs where navigation avoids
        # obstacles instead. The pan-angle rule and every lease/link/camera stop
        # stay regardless of this option.
        self.proximity_stop = proximity_stop
        # "servo": a physical pan head follows the commanded pan, and forward
        # drive is blocked while it is off-centre. "none": no pan hardware; the
        # headset pans digitally, so a valid commanded pan is accepted but never
        # moves anything or blocks forward drive (pan stays 0, the Uno gets no P).
        if pan_mount not in self.PAN_MOUNTS:
            raise ValueError(f"pan_mount must be one of {self.PAN_MOUNTS}")
        self.pan_mount = pan_mount
        # Optional navigation stack (jetson.nav.NavService). When configured it is
        # the only navigation: autonomy modes apply its velocity commands and hold
        # still while it is unavailable. Without it the reactive jetson.autonomy
        # policy runs as before.
        self.navigator = navigator
        self.autonomy = (Autonomy(proximity_stop=proximity_stop)
                         if detections is not None or navigator is not None else None)
        # Both default off: without them the wire protocol and PWM mixing are unchanged.
        self.profile = drive_profile
        self.allow_calibration = bool(allow_calibration)
        self.drive = None  # ("calibrate", (left, right)) or ("velocity", (v_mps, w_radps))
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
        # Same race for POST /nav/goal: permits issued before the goal was set may
        # still carry a zero-stick teleop heartbeat sent before the client switched
        # to mode "goto".
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
            self.scan_w, self.pan = w, round(pan) if self.pan_mount == "servo" else 0
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
            self.drive = None
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
                if self.navigator is not None:
                    self.navigator.disarm()  # a serial fault needs a fresh goal, not a silent resume
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
            if self.navigator is not None:
                self.navigator.disarm()
            return self.session_id

    def end(self, session_id):
        with self.lock:
            if self.session_id == session_id:
                self.stop("controller_disconnected")
                self.session_id = None
                self.permits.clear()
                if self.navigator is not None:
                    self.navigator.disarm()  # a go-to goal belongs to the controller that set it

    def update_range(self, cm):
        with self.lock:
            self.range_cm = cm if math.isfinite(cm) and 0 < cm <= 500 else None
            self.range_at = self.clock()

    def range_unsafe(self):
        """The ultrasonic proximity rule; never true when the option is off."""
        return self.proximity_stop and (self.range_cm is None or self.clock() - self.range_at >= self.RANGE_TTL
                                        or self.range_cm < 25)

    def forward_blocked(self):
        # The pan rule protects a servo pan mount and stays on with one; without
        # one, pan is always 0 here.
        return self.range_unsafe() or abs(self.pan) > 10

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
            elif mode == "calibrate" and not self.allow_calibration:
                self.stop("unsupported_mode")
                return False
            elif mode == "velocity" and self.profile is None:
                self.stop("unsupported_mode")
                return False
            elif mode not in ("teleop", "calibrate", "velocity") and (
                    not self.autonomy or mode not in ("explore", "seek", "goto")
                    or (mode == "goto" and self.navigator is None)):
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
            if mode in ("calibrate", "velocity"):
                return self._accept_drive(mode, message, seq, now, expires)
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
            if self.navigator is not None and (v or w):
                self.navigator.disarm()  # stick input disarms go-to-point
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
            if self.pan_mount == "none":
                pan = 0  # validated above; the headset pans digitally
            if round(pan) != self.pan:
                # A reading from a previous servo direction is not a forward
                # obstacle reading, even when the servo returns to the centre.
                self.range_cm = None
                self.range_at = -math.inf
            self.drive = None
            self.seq, self.v, self.w, self.pan = seq, float(v), float(w), round(pan)
            self.deadline = min(now + self.LEASE, expires)
            self.link_ms = round((self.LEASE - (expires - now)) * 1000, 1)
            self.reason = "active"
            return True

    def _accept_drive(self, mode, message, seq, now, expires):
        """Raw-PWM calibration or SI-unit velocity heartbeat; caller holds the lock.

        Same permit/deadman/camera/seq gates as teleop; autonomy is cancelled and
        the pan head is forced forward so the ultrasonic stop stays meaningful.
        """
        if mode == "calibrate":
            left, right = message.get("left_pwm"), message.get("right_pwm")
            if (type(left) is not int or type(right) is not int
                    or abs(left) > self.max_pwm or abs(right) > self.max_pwm):
                self.stop("invalid_command")
                return False
            drive = ("calibrate", (left, right))
        else:
            v, w = message.get("v_mps"), message.get("w_radps")
            if (any(type(x) not in (int, float) or not math.isfinite(x) for x in (v, w))
                    or abs(v) > 5 or abs(w) > 10):
                self.stop("invalid_command")
                return False
            drive = ("velocity", (float(v), float(w)))
        if self.scan_token is not None:
            self.stop("scan_override", require_release=False)
            return False
        if self.autonomy:
            self.autonomy.reset()
        self.stick_override = False
        if self.pan:
            self.range_cm = None
            self.range_at = -math.inf
        self.drive = drive
        self.seq, self.v, self.w, self.pan = seq, 0.0, 0.0, 0
        self.deadline = min(now + self.LEASE, expires)
        self.link_ms = round((self.LEASE - (expires - now)) * 1000, 1)
        self.reason = "active"
        return True

    def _drive_output(self, kind=None, values=None, max_pwm=None):
        if kind is None:
            kind, values = self.drive
        left, right = (0, 0)
        if self.connected:
            left, right = (values if kind == "calibrate"
                           else self.profile.wheel_pwm(*values, max_pwm=max_pwm or self.max_pwm))
        # Translation intent decides the range stop: a spin with slightly
        # unequal trimmed wheels must not be blocked by its own bias.
        translating = values[0] > 0 if kind == "velocity" else left + right > 0
        blocked = translating and (left > 0 or right > 0) and self.forward_blocked()
        if blocked:
            left = right = 0
            self.reason = "forward_blocked"
        if self.on_output:
            try:
                self.on_output((left + right) / 2 / self.max_pwm, (left - right) / 2 / self.max_pwm, blocked)
            except Exception:
                pass  # telemetry must never affect motor output
        return left, right, self.pan + 90

    def output(self):
        with self.lock:
            if self.deadline and self.clock() >= self.deadline:
                self.stop("lease_expired")
            autonomous = self.autonomy is not None and self.autonomy.mode != "teleop"
            if ((self.v or self.w or autonomous or self.drive is not None or self.scan_token is not None)
                    and not self.camera_is_ready()):
                self.stop("camera_stale")
            if self.drive is not None and self.deadline:
                return self._drive_output()
            v, w = self.v, self.w
            if self.scan_token is not None:
                v, w = 0.0, self.scan_w
                if w and self.range_unsafe():
                    self.stop("scan_range_stop")
                    v = w = 0.0
            engaged = bool(self.autonomy and self.autonomy.mode != "teleop" and self.deadline)
            if self.navigator is not None and self.autonomy:
                self.navigator.sync(self.autonomy.mode, self.autonomy.target_id, engaged)
            backed = engaged and self.navigator is not None and self.navigator.handles(self.autonomy.mode)
            if backed and self.autonomy.mode in ("explore", "seek"):
                snapshot = self.detections() if self.detections else {}
                usable = snapshot.get("fresh") and not snapshot.get("error")
                person = next((box for box in (snapshot.get("boxes", []) if usable else [])
                               if valid_person(box)), None)
                if self.autonomy.mode == "explore" and person is not None:
                    # Like the reactive policy: Explore hands over to Seek on the first person seen.
                    self.autonomy.mode, self.autonomy.target_id = "seek", person["id"]
                    self.navigator.sync("seek", person["id"], True)
                if self.autonomy.mode == "seek":
                    self.navigator.update_seek(snapshot)
            fault = self.navigator.sensor_fault() if backed else None
            if fault:
                # No fresh normal-tracking pose or measured depth of the current sensor session:
                # stop now, even with a live lease and fresh cmd_vel, and require a new press.
                self.stop(fault)
                v = w = 0.0
            elif engaged and self.navigator is not None and self.navigator.handles(self.autonomy.mode):
                # Nav2 drives this mode; a missing or stale velocity command stops.
                if self.profile is not None:  # calibrated: Nav2 m/s, rad/s straight to wheel PWM
                    command = self.navigator.command(si=True) or (0.0, 0.0)
                    top_v = self.profile.max_linear_mps(self.autonomy_max_pwm)
                    top_w = self.profile.max_angular_radps(self.autonomy_max_pwm)
                    return self._drive_output("velocity", (max(-top_v, min(top_v, command[0])),
                                                           max(-top_w, min(top_w, command[1]))),
                                              max_pwm=self.autonomy_max_pwm)
                command = self.navigator.command()
                v, w = command if command is not None else (0.0, 0.0)
            elif engaged and self.navigator is not None:
                v = w = 0.0  # Nav2 down: hold, never drive the reactive policy blind
            elif engaged:
                cm = self.range_cm if self.clock() - self.range_at < self.RANGE_TTL else None
                try:
                    v, w = self.autonomy.step(self.clock(), cm, self.detections() if self.detections else {})
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
            top = self.autonomy_max_pwm if engaged or self.scan_token is not None else self.max_pwm

            def pwm(value):
                if value == 0:
                    return 0
                magnitude = round(self.min_pwm + (top - self.min_pwm) * abs(value / scale))
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
                    "max_pwm": self.max_pwm, "min_pwm": self.min_pwm,
                    "drive_profile": self.profile is not None,
                    "calibration_allowed": self.allow_calibration,
                    "drive_mode": self.drive[0] if self.drive else "normalized",
                    **({"nav": self.navigator.nav_object(
                        self.autonomy.mode if self.autonomy else None,
                        bool(self.autonomy and self.autonomy.mode != "teleop" and self.deadline))}
                       if self.navigator is not None else {})}
