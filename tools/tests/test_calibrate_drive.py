import asyncio
import io
import json
import math
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from jetson.control import ControlService
from jetson.drive_profile import DriveProfile
from tools import calibrate_drive as cal


def transform(x, z, yaw):
    """ARKit camera-to-world, column-major; forward (-Z column) points along heading `yaw`."""
    fx, fz = math.cos(yaw), -math.sin(yaw)
    zx, zz = -fx, -fz
    rx, rz = zz, -zx
    return [rx, 0, rz, 0, 0, 1, 0, 0, zx, 0, zz, 0, x, 0.1, z, 1]


def spin_samples(rate, seconds=2.0, step=0.05):
    return [cal.Sample(i * step, 0, 0, cal.wrap_angle(0.2 + rate * i * step)) for i in range(int(seconds / step))]


class PoseMathTests(unittest.TestCase):
    def test_heading_is_counter_clockwise_from_above(self):
        self.assertAlmostEqual(cal.heading(1, 0), 0)
        self.assertAlmostEqual(cal.heading(0, -1), math.pi / 2)  # -Z is "forward" in ARKit; turning left from +X

    def test_sample_from_transform_round_trips_yaw_and_position(self):
        for yaw in (-2.5, -0.4, 0, 1.3, 3.0):
            s = cal.sample_from_transform(1.0, transform(0.5, -2.0, yaw))
            self.assertAlmostEqual(cal.wrap_angle(s.yaw - yaw), 0, places=6)
            self.assertEqual((s.x, s.z), (0.5, -2.0))

    def test_pitched_down_camera_falls_back_to_right_axis(self):
        t = transform(0, 0, 0.7)
        t[8], t[9], t[10] = 0.0, -1.0, 0.0  # looking straight down
        s = cal.sample_from_transform(0, t)
        self.assertAlmostEqual(cal.wrap_angle(s.yaw - 0.7), 0, places=6)

    def test_analyze_spin_signed_rate_across_wrap(self):
        run = cal.analyze_spin(spin_samples(-2.2, 3.0), 60)
        self.assertAlmostEqual(run.rate_radps, -2.2, places=3)
        self.assertIsNone(cal.analyze_spin(spin_samples(0.0), 60))

    def test_analyze_straight_speed_drift_and_camera_offset(self):
        speed, drift, offset = 0.3, 0.1, -0.35
        samples, x, z, theta = [], 0.0, 0.0, 0.0
        for i in range(60):
            samples.append(cal.Sample(i * 0.05, x, z, cal.wrap_angle(theta + 0.35)))
            theta += drift * 0.05
            x += speed * math.cos(theta) * 0.05
            z -= speed * math.sin(theta) * 0.05
        run = cal.analyze_straight(samples, 60)
        self.assertAlmostEqual(run.speed_mps, speed, delta=0.01)
        self.assertAlmostEqual(run.yaw_rate_radps, drift, places=3)
        self.assertAlmostEqual(run.camera_offset_rad, offset, delta=0.01)

    def test_stationary_and_short_runs_are_unusable(self):
        still = [cal.Sample(i * 0.05, 0, 0, 0) for i in range(60)]
        self.assertIsNone(cal.analyze_straight(still, 60))
        self.assertFalse(cal.moved(still))
        self.assertIsNone(cal.analyze_straight(still[:4], 60))


class SimulatedRoverTests(unittest.TestCase):
    def run_sim(self, **rover_args):
        rover = cal.SimulatedRover(**rover_args)
        plan = cal.Plan()
        profile, raw = asyncio.run(cal.calibrate(rover, plan, log=lambda *_: None))
        return rover, plan, profile, raw

    def test_fit_recovers_the_simulated_rover(self):
        rover, _, profile, _ = self.run_sim()
        self.assertAlmostEqual(profile.half_track_m, rover.half_track, delta=0.005)
        self.assertLessEqual(abs(profile.min_pwm_left - rover.floors[0]), 5)
        self.assertLessEqual(abs(profile.min_pwm_right - rover.floors[1]), 5)
        self.assertAlmostEqual(profile.trim_right / profile.trim_left, rover.gains[1] / rover.gains[0], delta=0.02)
        self.assertAlmostEqual(profile.camera_yaw_offset_rad, -rover.camera_yaw, delta=0.03)
        for pwm, mps in zip(profile.linear_pwm, profile.linear_mps):
            truth = rover.slope * (pwm - rover.offset_pwm) * (sum(rover.gains) / 2)
            self.assertAlmostEqual(mps, truth, delta=0.03 * truth)

    def test_calibrated_commands_drive_the_rover_at_the_requested_velocity(self):
        rover, _, profile, _ = self.run_sim()
        for v, w in ((0.3, 0.0), (0.2, 0.6), (0.0, 1.5), (0.35, -0.4), (0.0, -2.0)):
            left, right = profile.wheel_pwm(v, w)
            vl, vr = rover.wheel_speed(left, 0), rover.wheel_speed(right, 1)
            got_v, got_w = (vl + vr) / 2, (vr - vl) / (2 * rover.half_track)
            self.assertAlmostEqual(got_v, v, delta=0.025, msg=(v, w))
            self.assertAlmostEqual(got_w, w, delta=0.2, msg=(v, w))

    def test_rover_below_its_floor_does_not_move(self):
        rover = cal.SimulatedRover()
        self.assertEqual(rover.wheel_speed(30, 1), 0.0)  # right floor 31: the stiction the ramp must find

    def test_profile_survives_file_round_trip_and_server_loads_it(self):
        _, _, profile, raw = self.run_sim()
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "drive.json"
            cal.write_outputs(profile, raw, out)
            self.assertEqual(DriveProfile.load(out), profile)
            self.assertIn("straight", json.loads(out.with_suffix(".runs.json").read_text()))
            from jetson.mapping.server import create_app
            app = create_app(demo=True, simulate_rover=True, drive_profile=out)
            self.assertTrue(app is not None)

    def test_deadman_release_pauses_and_resumes(self):
        class Flaky(cal.SimulatedRover):
            calls = 0

            async def run(self, left, right, seconds):
                Flaky.calls += 1
                if Flaky.calls == 2:
                    raise cal.DeadmanReleased("released")
                return await super().run(left, right, seconds)

        waited = []

        async def wait():
            waited.append(1)

        samples = asyncio.run(cal.attempt(Flaky(), 40, 40, 1.0, wait, log=lambda *_: None))
        self.assertEqual(len(waited), 0)  # first call fine
        samples = asyncio.run(cal.attempt(Flaky(), 40, 40, 1.0, wait, log=lambda *_: None))
        self.assertEqual(len(waited), 1)
        self.assertGreater(len(samples), 5)

    def test_dead_wheel_fails_loudly(self):
        with self.assertRaises(cal.CalibrationError):
            asyncio.run(cal.calibrate(cal.SimulatedRover(floors=(500, 31)), cal.Plan(), log=lambda *_: None))


class CliTests(unittest.TestCase):
    def test_dry_run_touches_nothing(self):
        args = cal.parser().parse_args(["--dry-run", "--url", "http://203.0.113.1:1"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(asyncio.run(cal.main_async(args)), 0)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn("min-PWM ramp", out.getvalue())

    def test_simulate_writes_labelled_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "sim.json"
            args = cal.parser().parse_args(["--simulate", "--out", str(out)])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(asyncio.run(cal.main_async(args)), 0)
            self.assertEqual(DriveProfile.load(out).meta["source"], "simulated")

    def test_live_requires_url(self):
        with self.assertRaises(SystemExit):
            asyncio.run(cal.main_async(cal.parser().parse_args([])))


class PhysicsHealth:
    """Fake /health: integrates the simulated rover from the real server's applied PWM."""

    def __init__(self, control, rover, camera_yaw):
        self.control, self.rover, self.camera_yaw = control, rover, camera_yaw
        self.lock = threading.Lock()
        self.last = time.monotonic()
        self.x = self.z = self.theta = 0.0

    def __call__(self):
        with self.lock:
            now = time.monotonic()
            dt, self.last = now - self.last, now
            status = self.control.status()
            vl = self.rover.wheel_speed(status["left_pwm"], 0)
            vr = self.rover.wheel_speed(status["right_pwm"], 1)
            v, w = (vl + vr) / 2, (vr - vl) / (2 * self.rover.half_track)
            self.theta += w * dt
            self.x += v * math.cos(self.theta) * dt
            self.z -= v * math.sin(self.theta) * dt
            return {"fresh": True, "tracking": "normal", "age_ms": 5, "camera": True,
                    "transform": transform(self.x, self.z, self.theta + self.camera_yaw)}


class LiveWireTests(unittest.TestCase):
    """Real ControlService + WebSocket + LiveRover; only the pose source is physics."""

    def serve(self, allow):
        self.rover = cal.SimulatedRover(noise=0.0)
        control = ControlService(simulate=True, max_pwm=100, camera_ready=lambda: True, allow_calibration=allow)
        physics = PhysicsHealth(control, self.rover, self.rover.camera_yaw)
        app = FastAPI()
        app.include_router(control.router)
        app.add_api_route("/health", lambda: physics(), methods=["GET"])
        control.start()
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started and control.status()["rover"]:
                break
            time.sleep(0.05)
        self.addCleanup(lambda: (setattr(server, "should_exit", True), thread.join(5), control.close()))
        return f"http://127.0.0.1:{port}", control

    def test_refuses_server_without_allow_calibration(self):
        origin, _ = self.serve(allow=False)

        async def go():
            rover = cal.LiveRover(origin, lambda: True, log=lambda *_: None)
            try:
                await rover.start()
            finally:
                await rover.close()

        with self.assertRaises(cal.CalibrationError) as caught:
            asyncio.run(go())
        self.assertIn("--allow-calibration", str(caught.exception))

    def test_full_calibration_over_the_real_control_path(self):
        origin, control = self.serve(allow=True)
        plan = cal.Plan(levels=2, repeats=2, run_s=1.5, spin_s=1.3, hold_s=0.45, rest_s=0.15, ramp_step=5)

        async def go():
            rover = cal.LiveRover(origin, lambda: True, log=lambda *_: None)
            try:
                await rover.start()
                return await cal.calibrate(rover, plan, rover.wait_deadman, lambda *_: None)
            finally:
                await rover.close()

        profile, raw = asyncio.run(go())
        self.assertAlmostEqual(profile.half_track_m, self.rover.half_track, delta=0.012)
        self.assertLessEqual(abs(profile.min_pwm_left - self.rover.floors[0]), 5)
        self.assertLessEqual(abs(profile.min_pwm_right - self.rover.floors[1]), 5)
        self.assertAlmostEqual(profile.camera_yaw_offset_rad, -self.rover.camera_yaw, delta=0.06)
        self.assertAlmostEqual(profile.trim_right / profile.trim_left, 0.94, delta=0.04)
        # The tool must leave the rover stopped and released.
        time.sleep(0.4)
        status = control.status()
        self.assertEqual((status["left_pwm"], status["right_pwm"]), (0, 0))

    def test_released_deadman_stops_the_rover(self):
        origin, control = self.serve(allow=True)
        held = [True]

        async def go():
            rover = cal.LiveRover(origin, lambda: held[0], log=lambda *_: None)
            try:
                await rover.start()
                task = asyncio.create_task(rover.run(-40, -40, 3.0))
                await asyncio.sleep(0.8)
                moving = (control.status()["left_pwm"], control.status()["right_pwm"])
                held[0] = False
                with self.assertRaises(cal.DeadmanReleased):
                    await task
                await asyncio.sleep(0.1)
                return moving, (control.status()["left_pwm"], control.status()["right_pwm"])
            finally:
                await rover.close()

        moving, stopped = asyncio.run(go())
        self.assertEqual(moving, (-40, -40))
        self.assertEqual(stopped, (0, 0))


if __name__ == "__main__":
    unittest.main()
