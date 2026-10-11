import json
import math
import tempfile
import unittest
from pathlib import Path

from jetson.control.state import ControlState
from jetson.drive_profile import (DriveProfile, SpinRun, StraightRun, fit_profile, interpolate, invert,
                                  linear_fit, pool_adjacent_violators, unwrap)


def make_profile(**overrides):
    values = dict(max_pwm=100, min_pwm_left=24, min_pwm_right=30,
                  linear_pwm=(40, 60, 80, 100), linear_mps=(0.16, 0.29, 0.42, 0.54),
                  angular_pwm=(40, 100), angular_radps=(1.4, 4.7), half_track_m=0.115,
                  trim_left=1.03, trim_right=0.97, camera_yaw_offset_rad=-0.35)
    values.update(overrides)
    return DriveProfile(**values)


class MathTests(unittest.TestCase):
    def test_pool_adjacent_violators(self):
        self.assertEqual(pool_adjacent_violators([1, 3, 2, 4]), [1, 2.5, 2.5, 4])
        self.assertEqual(pool_adjacent_violators([5, 4, 3]), [4, 4, 4])

    def test_interpolate_and_invert_clamp(self):
        xs, ys = [10, 20, 30], [1, 3, 4]
        self.assertEqual(interpolate(15, xs, ys), 2)
        self.assertEqual((interpolate(0, xs, ys), interpolate(99, xs, ys)), (1, 4))
        self.assertAlmostEqual(invert(3.5, xs, ys), 25)
        self.assertEqual((invert(0, xs, ys), invert(9, xs, ys)), (10, 30))

    def test_linear_fit_exact(self):
        slope, intercept, r2 = linear_fit([0, 1, 2, 3], [1, 3, 5, 7])
        self.assertAlmostEqual((slope, intercept, r2), (2, 1, 1))

    def test_unwrap_crosses_pi(self):
        angles = [3.0, 3.1, -3.1, -3.0]
        out = unwrap(angles)
        self.assertTrue(all(b > a for a, b in zip(out, out[1:])))


class ProfileTests(unittest.TestCase):
    def test_round_trip_file(self):
        profile = make_profile(meta={"source": "test"})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "p.json"
            profile.save(path)
            self.assertEqual(DriveProfile.load(path), profile)
            self.assertEqual(json.loads(path.read_text())["version"], 1)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ["p.json"])

    def test_rejects_bad_profiles(self):
        good = make_profile().to_dict()
        for mutate in (lambda d: d.update(version=2),
                       lambda d: d["linear"].update(mps=[0.5, 0.3, 0.4, 0.5]),
                       lambda d: d["min_pwm"].update(left=0),
                       lambda d: d["min_pwm"].update(right=50),  # above first curve point
                       lambda d: d.update(half_track_m=0),
                       lambda d: d["trim"].update(left=9),
                       lambda d: d.pop("trim")):
            data = json.loads(json.dumps(good))
            mutate(data)
            with self.assertRaises(ValueError):
                DriveProfile.from_dict(data)

    def test_command_maps_back_to_requested_velocity(self):
        profile = make_profile()
        for v in (0.2, 0.3, 0.4, -0.3):
            for w in (0, 0.5, -1.0):
                left, right = profile.wheel_pwm(v, w)
                got_v, got_w = profile.body_velocity(left, right)
                self.assertAlmostEqual(got_v, v, delta=0.02, msg=(v, w))
                self.assertAlmostEqual(got_w, w, delta=0.12, msg=(v, w))

    def test_trim_biases_the_slower_side_up(self):
        profile = make_profile(trim_left=1.0, trim_right=0.9)
        left, right = profile.wheel_pwm(0.4, 0)
        self.assertGreater(right, left)
        self.assertAlmostEqual(profile.body_velocity(left, right)[1], 0, delta=0.05)

    def test_floor_applies_per_side_and_deadband(self):
        profile = make_profile(min_pwm_left=24, min_pwm_right=30)
        left, right = profile.wheel_pwm(0.01, 0)  # below 10% of slowest tested speed
        self.assertEqual((left, right), (0, 0))
        left, right = profile.wheel_pwm(0.10, 0)  # between floor and first curve point
        self.assertTrue(24 <= left < 40 and 30 <= right < 40)
        self.assertEqual(profile.wheel_pwm(0, 0), (0, 0))

    def test_saturation_keeps_curvature_and_respects_cap(self):
        profile = make_profile(trim_left=1.0, trim_right=1.0)
        left, right = profile.wheel_pwm(2.0, 1.0, max_pwm=80)
        self.assertLessEqual(max(left, right), 80)
        self.assertGreater(left, 0)
        self.assertGreater(right, left)
        ratio = profile.body_velocity(left, right)
        self.assertAlmostEqual(ratio[1] / ratio[0], 0.5, delta=0.1)

    def test_in_place_spin_signs_ros(self):
        left, right = make_profile().wheel_pwm(0, 1.0)  # +w = counter-clockwise: right wheel forward
        self.assertLess(left, 0)
        self.assertGreater(right, 0)

    def test_in_place_turns_use_the_measured_spin_floor(self):
        # 2026-10-10 rig: floors L70/R40 from straight runs; spins measured from PWM 80.
        profile = make_profile(max_pwm=255, min_pwm_left=70, min_pwm_right=40,
                               linear_pwm=(80, 120, 160), linear_mps=(0.268, 0.416, 0.588),
                               angular_pwm=(80, 120, 160), angular_radps=(0.82, 2.5, 3.83),
                               half_track_m=0.166, trim_left=0.975, trim_right=1.025)
        left, right = profile.wheel_pwm(0, 0.82)  # straight-line curve alone gives about L-75/R+60
        self.assertEqual((left, right), (-80, 80))
        left, right = profile.wheel_pwm(0, -2.2)
        self.assertTrue(left > 80 and right < -80)
        self.assertLess(profile.wheel_pwm(0.10, 0)[1], 80)  # straight driving keeps its own floors
        self.assertEqual(profile.wheel_pwm(0, 0.82, max_pwm=75), (-75, 75))  # never above the cap

    def test_loaded_scale_saturates_cruise_and_spins_and_keeps_floors(self):
        # 2026-10-11 rig: loaded, ARKit showed 0.32 of the profiled speed at the same PWM.
        profile = make_profile(max_pwm=255, min_pwm_left=70, min_pwm_right=40,
                               linear_pwm=(80, 120, 160, 255), linear_mps=(0.268, 0.416, 0.588, 0.95),
                               angular_pwm=(80, 120, 160), angular_radps=(0.82, 2.5, 3.83),
                               half_track_m=0.166, trim_left=0.975, trim_right=1.025)
        self.assertEqual(profile.wheel_pwm(0, 2.2), (-109, 104))  # what the floor test spun at
        loaded = profile.scale_linear(0.32, "navwatch2-0232")
        self.assertGreaterEqual(min(map(abs, loaded.wheel_pwm(0.45, 0))), 240)
        self.assertGreaterEqual(min(map(abs, loaded.wheel_pwm(0, 2.2))), 240)
        self.assertGreater(min(loaded.wheel_pwm(0.15, 0)), 120)  # the AUTO floor is well above the PWM floors
        self.assertEqual(loaded.wheel_pwm(0, 0.3)[1], 80)  # the measured spin floor stays
        self.assertEqual((loaded.min_pwm_left, loaded.min_pwm_right, loaded.linear_pwm, loaded.angular_radps),
                         (70, 40, profile.linear_pwm, profile.angular_radps))
        self.assertAlmostEqual(loaded.max_linear_mps(), 0.304)
        self.assertEqual(DriveProfile.from_dict(loaded.to_dict()), loaded)
        self.assertEqual(loaded.meta["linear_scale"]["unscaled_mps"], list(profile.linear_mps))
        with self.assertRaises(ValueError):
            profile.scale_linear(0, "none")

    def test_non_finite_rejected(self):
        with self.assertRaises(ValueError):
            make_profile().wheel_pwm(float("nan"), 0)


class FitTests(unittest.TestCase):
    def test_fit_recovers_synthetic_truth(self):
        k, drift_ratio = 0.12, 0.04  # right wheel 4% fast relative to the mean
        straight = [StraightRun(p, 0.006 * p, k * 0 + drift_ratio * 0.006 * p / k, 0.3) for p in (40, 60, 80, 100)]
        spins = [SpinRun(p, s * 0.006 * p / k) for p in (40, 100) for s in (1, -1)]
        profile = fit_profile(24, 30, straight, spins, 100)
        self.assertAlmostEqual(profile.half_track_m, k, places=3)
        self.assertAlmostEqual(profile.trim_right - profile.trim_left, 2 * drift_ratio, places=3)
        self.assertAlmostEqual(profile.camera_yaw_offset_rad, 0.3, places=3)
        self.assertAlmostEqual(profile.max_linear_mps(), 0.6, places=3)

    def test_fit_needs_motion(self):
        with self.assertRaises(ValueError):
            fit_profile(24, 30, [StraightRun(50, 0.2, 0, 0)], [SpinRun(50, 1)], 100)
        with self.assertRaises(ValueError):
            fit_profile(24, 30, [StraightRun(50, 0.2, 0, 0), StraightRun(60, 0.3, 0, 0)], [], 100)


class ControlModeTests(unittest.TestCase):
    def build(self, **kwargs):
        self.now = 10.0
        state = ControlState(clock=lambda: self.now, **kwargs)
        state.set_connected(True)
        self.session = state.begin()
        state.update_range(150)
        self.seq = 0
        self.send(state, deadman=False)
        return state

    def send(self, state, **values):
        self.seq += 1
        message = {"session_id": self.session, "permit": state.status(issue_permit=True)["permit"],
                   "seq": self.seq, "deadman": True, "mode": "teleop", "v": 0, "w": 0, "pan": 0,
                   "video_fresh": True, "video_age_ms": 0}
        message.update(values)
        return state.accept(message, self.session)

    def test_default_server_rejects_new_modes_and_status_is_unchanged(self):
        state = self.build()
        self.assertFalse(self.send(state, mode="velocity", v_mps=0.2, w_radps=0))
        self.assertEqual(state.reason, "unsupported_mode")
        self.assertEqual(state.output()[:2], (0, 0))
        self.send(state, deadman=False)
        self.assertFalse(self.send(state, mode="calibrate", left_pwm=40, right_pwm=40))
        self.assertEqual(state.reason, "unsupported_mode")
        status = state.status()
        self.assertEqual((status["drive_profile"], status["calibration_allowed"], status["drive_mode"]),
                         (False, False, "normalized"))

    def test_teleop_pwm_identical_with_profile_loaded(self):
        state = self.build(drive_profile=make_profile())
        self.assertTrue(self.send(state, v=0.5, w=0))
        self.assertEqual(state.output()[:2], (50, 50))

    def test_velocity_mode_uses_profile(self):
        profile = make_profile()
        state = self.build(drive_profile=profile)
        self.assertTrue(self.send(state, mode="velocity", v_mps=0.3, w_radps=0.4))
        left, right, pan = state.output()
        self.assertEqual((left, right), profile.wheel_pwm(0.3, 0.4, max_pwm=100))
        self.assertEqual(pan, 90)
        self.assertEqual(state.status()["drive_mode"], "velocity")

    def test_velocity_mode_still_has_every_safety_gate(self):
        state = self.build(drive_profile=make_profile())
        self.assertTrue(self.send(state, mode="velocity", v_mps=0.3, w_radps=0))
        state.update_range(10)
        self.assertEqual(state.output()[:2], (0, 0))
        self.assertEqual(state.reason, "forward_blocked")
        state.update_range(150)
        self.now += 0.3  # lease expired
        self.assertEqual(state.output()[:2], (0, 0))
        self.assertTrue(state.requires_release)
        self.send(state, deadman=False)
        self.assertFalse(self.send(state, mode="velocity", v_mps=float("nan"), w_radps=0))
        self.assertFalse(self.send(state, deadman=False) and self.send(state, mode="velocity", v_mps=9, w_radps=0))

    def test_velocity_spin_not_blocked_by_missing_range(self):
        state = self.build(drive_profile=make_profile())
        state.range_cm = None
        self.assertTrue(self.send(state, mode="velocity", v_mps=0, w_radps=1.0))
        left, right, _ = state.output()
        self.assertLess(left, 0)
        self.assertGreater(right, 0)

    def test_calibrate_raw_pwm_and_bounds(self):
        state = self.build(allow_calibration=True, max_pwm=100, min_pwm=20)
        self.assertTrue(self.send(state, mode="calibrate", left_pwm=33, right_pwm=0))
        self.assertEqual(state.output()[:2], (33, 0))  # min_pwm floor does not rewrite raw values
        self.assertEqual(state.status()["drive_mode"], "calibrate")
        self.assertFalse(self.send(state, mode="calibrate", left_pwm=101, right_pwm=0))
        self.assertEqual(state.output()[:2], (0, 0))
        self.send(state, deadman=False)
        self.assertFalse(self.send(state, mode="calibrate", left_pwm=10.5, right_pwm=0))
        self.send(state, deadman=False)
        self.assertFalse(self.send(state, mode="calibrate", left_pwm=True, right_pwm=0))

    def test_calibrate_forward_gate_and_reverse_and_spin(self):
        state = self.build(allow_calibration=True)
        state.update_range(10)
        self.assertTrue(self.send(state, mode="calibrate", left_pwm=40, right_pwm=40))
        self.assertEqual(state.output()[:2], (0, 0))
        self.assertTrue(self.send(state, mode="calibrate", left_pwm=-40, right_pwm=-40))
        self.assertEqual(state.output()[:2], (-40, -40))
        self.assertTrue(self.send(state, mode="calibrate", left_pwm=-40, right_pwm=40))
        self.assertEqual(state.output()[:2], (-40, 40))

    def test_stale_camera_stops_calibration_motion(self):
        ready = [True]
        state = self.build(allow_calibration=True, camera_ready=lambda: ready[0])
        self.assertTrue(self.send(state, mode="calibrate", left_pwm=-40, right_pwm=-40))
        ready[0] = False
        self.assertEqual(state.output()[:2], (0, 0))
        self.assertEqual(state.reason, "camera_stale")

    def test_lease_and_disconnect_stop_calibration(self):
        state = self.build(allow_calibration=True)
        self.assertTrue(self.send(state, mode="calibrate", left_pwm=-40, right_pwm=-40))
        self.now += 0.26
        self.assertEqual(state.output()[:2], (0, 0))
        state = self.build(allow_calibration=True)
        self.assertTrue(self.send(state, mode="calibrate", left_pwm=-40, right_pwm=-40))
        state.set_connected(False)
        self.assertEqual(state.output()[:2], (0, 0))


if __name__ == "__main__":
    unittest.main()
