"""Measured drive calibration: PWM <-> m/s, rad/s, per-side floors and trim.

Stdlib only so the Mac-side tool and the Jetson server share one definition.
Convention follows ROS `cmd_vel`: +v forward, +w counter-clockwise (left turn).
The legacy normalized control path (`w` positive turns right) is unchanged.

Model: each wheel's ground speed is `gain * curve(|pwm|)`, where `curve` is the
measured forward PWM->m/s curve. A body command (v, w) asks for wheel speeds
`v -/+ half_track * w` (left/right); `half_track` is fitted from in-place spins,
so skid slip is folded into it. Below a side's minimum moving PWM nothing moves,
so a requested nonzero speed is raised to that floor.
"""

import json
import math
import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA_VERSION = 1


def pool_adjacent_violators(values):
    """Nearest nondecreasing sequence in the least-squares sense."""
    blocks = []  # [sum, count]
    for value in values:
        blocks.append([float(value), 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            total, count = blocks.pop()
            blocks[-1][0] += total
            blocks[-1][1] += count
    out = []
    for total, count in blocks:
        out.extend([total / count] * count)
    return out


def interpolate(x, xs, ys):
    """Piecewise-linear y(x), clamped at both ends. xs must increase."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    for i in range(1, len(xs)):
        if x <= xs[i]:
            span = xs[i] - xs[i - 1]
            return ys[i - 1] + (ys[i] - ys[i - 1]) * ((x - xs[i - 1]) / span)
    return ys[-1]


def invert(y, xs, ys):
    """x for a nondecreasing curve, clamped; flat spans resolve to the lowest x."""
    if y <= ys[0]:
        return xs[0]
    if y >= ys[-1]:
        return xs[-1]
    for i in range(1, len(xs)):
        if y <= ys[i]:
            rise = ys[i] - ys[i - 1]
            if rise <= 0:
                return xs[i - 1]
            return xs[i - 1] + (xs[i] - xs[i - 1]) * ((y - ys[i - 1]) / rise)
    return xs[-1]


def linear_fit(xs, ys):
    """Least squares y = slope*x + intercept; returns (slope, intercept, r2)."""
    n = len(xs)
    if n < 2:
        raise ValueError("Need at least two samples to fit a line")
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ValueError("Samples have no spread in x")
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    return slope, intercept, (1.0 if ss_tot == 0 else 1 - ss_res / ss_tot)


def wrap_angle(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def unwrap(angles):
    out, offset = [], 0.0
    for i, angle in enumerate(angles):
        if i:
            step = angle - angles[i - 1]
            offset -= 2 * math.pi * round(step / (2 * math.pi))
        out.append(angle + offset)
    return out


def circular_mean(angles):
    return math.atan2(sum(math.sin(a) for a in angles), sum(math.cos(a) for a in angles))


def _is_curve(xs, ys):
    return (len(xs) == len(ys) >= 1
            and all(isinstance(v, (int, float)) and math.isfinite(v) for v in (*xs, *ys))
            and all(b > a for a, b in zip(xs, xs[1:])) and all(b >= a for a, b in zip(ys, ys[1:]))
            and xs[0] > 0 and ys[0] > 0)


@dataclass(frozen=True)
class DriveProfile:
    max_pwm: int
    min_pwm_left: int
    min_pwm_right: int
    linear_pwm: tuple
    linear_mps: tuple
    angular_pwm: tuple
    angular_radps: tuple
    half_track_m: float
    trim_left: float = 1.0
    trim_right: float = 1.0
    camera_yaw_offset_rad: float = 0.0
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        for name in ("linear_pwm", "linear_mps", "angular_pwm", "angular_radps"):
            object.__setattr__(self, name, tuple(float(v) for v in getattr(self, name)))
        if not _is_curve(self.linear_pwm, self.linear_mps) or not _is_curve(self.angular_pwm, self.angular_radps):
            raise ValueError("Calibration curves must be positive, finite, and nondecreasing")
        if not (isinstance(self.max_pwm, int) and 1 <= self.max_pwm <= 255):
            raise ValueError("max_pwm must be an integer 1..255")
        for floor in (self.min_pwm_left, self.min_pwm_right):
            if not (isinstance(floor, int) and 0 < floor <= self.max_pwm):
                raise ValueError("Per-side minimum PWM must be an integer in 1..max_pwm")
        if min(self.linear_pwm[0], self.angular_pwm[0]) < max(self.min_pwm_left, self.min_pwm_right):
            raise ValueError("Curves must start at or above the per-side minimum PWM")
        if not (math.isfinite(self.half_track_m) and self.half_track_m > 0):
            raise ValueError("half_track_m must be positive")
        for gain in (self.trim_left, self.trim_right):
            if not (math.isfinite(gain) and 0.25 <= gain <= 4):
                raise ValueError("Trim gains must be within 0.25..4")
        if not (math.isfinite(self.camera_yaw_offset_rad) and abs(self.camera_yaw_offset_rad) <= math.pi):
            raise ValueError("camera_yaw_offset_rad must be within -pi..pi")

    # -- derived limits ----------------------------------------------------
    def cap_pwm(self, max_pwm=None):
        return min(self.max_pwm, max_pwm or self.max_pwm, int(self.linear_pwm[-1]))

    def max_linear_mps(self, max_pwm=None):
        return interpolate(self.cap_pwm(max_pwm), self.linear_pwm, self.linear_mps)

    def max_angular_radps(self, max_pwm=None):
        return interpolate(min(self.cap_pwm(max_pwm), self.angular_pwm[-1]), self.angular_pwm, self.angular_radps)

    # -- command mapping (cmd_vel -> PWM) ----------------------------------
    def _side_pwm(self, speed, floor, cap):
        magnitude = abs(speed)
        slowest = self.linear_mps[0]
        if magnitude < 0.1 * slowest:
            return 0
        if magnitude < slowest:
            pwm = floor + (self.linear_pwm[0] - floor) * (magnitude / slowest)
        else:
            pwm = invert(magnitude, self.linear_pwm, self.linear_mps)
        pwm = int(min(cap, max(floor, round(pwm))))
        return pwm if speed > 0 else -pwm

    def wheel_pwm(self, v_mps, w_radps, max_pwm=None):
        """(left, right) signed PWM for a body velocity; saturates keeping curvature."""
        if not (math.isfinite(v_mps) and math.isfinite(w_radps)):
            raise ValueError("Velocity must be finite")
        cap = self.cap_pwm(max_pwm)
        left = (v_mps - self.half_track_m * w_radps) / self.trim_left
        right = (v_mps + self.half_track_m * w_radps) / self.trim_right
        top = interpolate(cap, self.linear_pwm, self.linear_mps)
        biggest = max(abs(left), abs(right))
        if biggest > top:
            left, right = left * top / biggest, right * top / biggest
        left_pwm = self._side_pwm(left, self.min_pwm_left, cap)
        right_pwm = self._side_pwm(right, self.min_pwm_right, cap)
        if left_pwm * right_pwm < 0:
            # Opposed wheels (in-place turn, very tight arc) scrub: the straight-line floors
            # stall under payload. Use at least the lowest PWM the calibration saw spin.
            spin_floor = min(cap, int(self.angular_pwm[0]))
            left_pwm = int(math.copysign(max(abs(left_pwm), spin_floor), left_pwm))
            right_pwm = int(math.copysign(max(abs(right_pwm), spin_floor), right_pwm))
        return left_pwm, right_pwm

    # -- forward model (PWM -> body velocity), used by tests and analysis ---
    def _wheel_speed(self, pwm, floor, gain):
        magnitude = abs(pwm)
        if magnitude < floor:
            return 0.0
        slowest = self.linear_mps[0]
        if magnitude < self.linear_pwm[0]:
            speed = slowest * (magnitude - floor) / max(self.linear_pwm[0] - floor, 1e-9)
        else:
            speed = interpolate(magnitude, self.linear_pwm, self.linear_mps)
        return gain * speed * (1 if pwm > 0 else -1)

    def body_velocity(self, left_pwm, right_pwm):
        left = self._wheel_speed(left_pwm, self.min_pwm_left, self.trim_left)
        right = self._wheel_speed(right_pwm, self.min_pwm_right, self.trim_right)
        return (left + right) / 2, (right - left) / (2 * self.half_track_m)

    # -- persistence -------------------------------------------------------
    def to_dict(self):
        return {
            "version": SCHEMA_VERSION,
            "max_pwm": self.max_pwm,
            "min_pwm": {"left": self.min_pwm_left, "right": self.min_pwm_right},
            "linear": {"pwm": list(self.linear_pwm), "mps": list(self.linear_mps)},
            "angular": {"pwm": list(self.angular_pwm), "radps": list(self.angular_radps)},
            "half_track_m": self.half_track_m,
            "trim": {"left": self.trim_left, "right": self.trim_right},
            "camera_yaw_offset_rad": self.camera_yaw_offset_rad,
            "limits": {"max_linear_mps": self.max_linear_mps(), "max_angular_radps": self.max_angular_radps()},
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data):
        try:
            if data["version"] != SCHEMA_VERSION:
                raise ValueError(f"Unsupported drive profile version {data['version']!r}")
            return cls(max_pwm=data["max_pwm"], min_pwm_left=data["min_pwm"]["left"],
                       min_pwm_right=data["min_pwm"]["right"],
                       linear_pwm=data["linear"]["pwm"], linear_mps=data["linear"]["mps"],
                       angular_pwm=data["angular"]["pwm"], angular_radps=data["angular"]["radps"],
                       half_track_m=data["half_track_m"], trim_left=data["trim"]["left"],
                       trim_right=data["trim"]["right"],
                       camera_yaw_offset_rad=data["camera_yaw_offset_rad"], meta=dict(data.get("meta", {})))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"Malformed drive profile: {exc!r}") from exc

    def save(self, path):
        path = Path(path)
        text = json.dumps(self.to_dict(), indent=2) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp") as handle:
            handle.write(text)
        Path(handle.name).replace(path)  # never leave a half-written profile for the server

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text()))


# -- fitting -----------------------------------------------------------------

@dataclass(frozen=True)
class StraightRun:
    pwm: int
    speed_mps: float          # along-track, > 0
    yaw_rate_radps: float     # ROS sign: + = drifting left
    camera_offset_rad: float | None = None


@dataclass(frozen=True)
class SpinRun:
    pwm: int
    rate_radps: float         # signed, + = counter-clockwise


def _curve(points):
    """{pwm: [values]} -> monotone (pwm tuple, mean-value tuple)."""
    pwms = sorted(points)
    means = pool_adjacent_violators([statistics.fmean(points[p]) for p in pwms])
    return pwms, means


def fit_profile(min_pwm_left, min_pwm_right, straight_runs, spin_runs, max_pwm, meta=None):
    straight = [r for r in straight_runs if r.speed_mps > 0]
    spins = [r for r in spin_runs if abs(r.rate_radps) > 0]
    if len(straight) < 2 or len({r.pwm for r in straight}) < 2:
        raise ValueError("Need straight runs at two or more PWM levels that actually moved")
    if not spins:
        raise ValueError("Need at least one in-place spin that actually turned")
    lin: dict = {}
    for run in straight:
        lin.setdefault(run.pwm, []).append(run.speed_mps)
    ang: dict = {}
    for run in spins:
        ang.setdefault(run.pwm, []).append(abs(run.rate_radps))
    lin_pwm, lin_mps = _curve(lin)
    ang_pwm, ang_radps = _curve(ang)
    # A spin drives both wheels at +/-pwm, so wheel speed = curve(pwm) and
    # rate = wheel speed / half_track.
    tracks = [interpolate(p, lin_pwm, lin_mps) / r for p, r in zip(ang_pwm, ang_radps)]
    half_track = statistics.median(tracks)
    # Left faster than right makes the body yaw right (negative ROS yaw rate).
    ratios = [half_track * run.yaw_rate_radps / run.speed_mps for run in straight]
    drift = statistics.fmean(ratios)
    trim_left, trim_right = (min(1.5, max(0.5, 1 - drift)), min(1.5, max(0.5, 1 + drift)))
    offsets = [r.camera_offset_rad for r in straight if r.camera_offset_rad is not None]
    details = dict(meta or {})
    details.update(straight_runs=len(straight), spin_runs=len(spins),
                   half_track_spread_m=round(max(tracks) - min(tracks), 4),
                   spin_ccw_mean_radps=_signed_mean(spins, 1), spin_cw_mean_radps=_signed_mean(spins, -1))
    return DriveProfile(max_pwm=max_pwm, min_pwm_left=min_pwm_left, min_pwm_right=min_pwm_right,
                        linear_pwm=lin_pwm, linear_mps=lin_mps, angular_pwm=ang_pwm, angular_radps=ang_radps,
                        half_track_m=half_track, trim_left=trim_left, trim_right=trim_right,
                        camera_yaw_offset_rad=circular_mean(offsets) if offsets else 0.0, meta=details)


def _signed_mean(spins, sign):
    rates = [abs(r.rate_radps) for r in spins if r.rate_radps * sign > 0]
    return round(statistics.fmean(rates), 4) if rates else None
