import unittest

from jetson.autonomy import Autonomy
from jetson.control.state import ControlState


def detection(x=.4, identifier=7, age_ms=0):
    return dict(fresh=True, error=None, age_ms=age_ms,
                boxes=[dict(id=identifier, cls="person", x=x, w=.2)])


class AutonomyPolicyTests(unittest.TestCase):
    def test_seek_centres_target_tapers_speed_and_stays_stopped_at_sixty_cm(self):
        policy = Autonomy()
        policy.select("seek", 7, 10)
        v, w = policy.step(10, 160, detection(.7))
        self.assertGreater(v, 0)
        self.assertGreater(w, 0)  # target right -> right turn
        near, left = policy.step(10.1, 80, detection(.1))
        self.assertLess(near, v)
        self.assertLess(left, 0)
        self.assertEqual(policy.step(10.2, 60, detection()), (0, 0))
        self.assertEqual(policy.phase, "arrived")
        self.assertEqual(policy.step(10.3, 150, detection()), (0, 0))

    def test_missing_target_stops_immediately_and_latches_lost_after_one_second(self):
        policy = Autonomy()
        policy.select("seek", 7, 10)
        policy.step(10, 160, detection())
        empty = dict(fresh=True, error=None, age_ms=0, boxes=[])
        self.assertEqual(policy.step(10.5, 160, empty), (0, 0))
        self.assertEqual(policy.phase, "target_missing")
        self.assertEqual(policy.step(11, 160, empty), (0, 0))
        self.assertEqual(policy.phase, "target_lost")
        self.assertEqual(policy.step(11.1, 160, detection()), (0, 0))

    def test_stale_results_and_detector_errors_cannot_steer(self):
        for snapshot in (dict(detection(), fresh=False), dict(detection(), error="failed"),
                         detection(age_ms=1000), detection(identifier=8)):
            with self.subTest(snapshot=snapshot):
                policy = Autonomy()
                policy.select("seek", 7, 10)
                self.assertEqual(policy.step(10, 120, snapshot), (0, 0))

    def test_explore_obstacle_and_timeout_turn_bounded_then_handover_to_seek(self):
        empty = dict(fresh=True, error=None, age_ms=0, boxes=[])
        for trigger_time, range_cm in ((10.1, 65), (14, 120)):
            with self.subTest(trigger_time=trigger_time):
                policy = Autonomy(choose_turn=lambda: -1)
                policy.select("explore", None, 10)
                self.assertEqual(policy.step(10, 120, empty), (.25, 0))
                self.assertEqual(policy.step(trigger_time, range_cm, empty), (0, -.35))
                self.assertEqual(policy.phase, "turning")
                self.assertEqual(policy.step(trigger_time + 1.01, 120, empty), (.25, 0))
                self.assertEqual(policy.step(trigger_time + 1.1, 120, detection()), (.3, 0))
                self.assertEqual((policy.mode, policy.target_id), ("seek", 7))
                policy.select("explore", None, trigger_time + 1.2)  # operator's heartbeat
                self.assertEqual(policy.mode, "seek")

    def test_unknown_range_and_estop_stop_all_autonomous_motion(self):
        for mode in ("explore", "seek"):
            for cm in (None, float("nan"), 0, 20):
                with self.subTest(mode=mode, cm=cm):
                    policy = Autonomy()
                    policy.select(mode, 7 if mode == "seek" else None, 10)
                    self.assertEqual(policy.step(10, cm, detection(.7)), (0, 0))

    def test_malformed_person_box_is_not_a_motion_target(self):
        for x, width in ((float("nan"), .2), (.2, -1), (.9, .5), (".4", .2)):
            with self.subTest(x=x, width=width):
                policy = Autonomy()
                policy.select("seek", 7, 10)
                snapshot = detection(x)
                snapshot["boxes"][0]["w"] = width
                self.assertEqual(policy.step(10, 120, snapshot), (0, 0))


class AutonomyControlTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.snapshot = detection()
        self.state = ControlState(simulated=True, max_pwm=80, clock=lambda: self.now,
                                  detections=lambda: self.snapshot)
        self.state.set_connected(True)
        self.session = self.state.begin()
        self.seq = 0
        self.send(deadman=False)

    def send(self, refresh_range=True, **values):
        if refresh_range:
            self.state.update_range(120)
        status = self.state.status(issue_permit=True)
        self.seq += 1
        message = dict(session_id=self.session, permit=status["permit"], seq=self.seq,
                       deadman=True, mode="seek", target_id=7, v=0, w=0, pan=0,
                       video_fresh=True, video_age_ms=0)
        message.update(values)
        return self.state.accept(message, self.session)

    def test_seek_uses_control_ceiling_and_stick_override_latches_teleop(self):
        self.assertTrue(self.send())
        self.assertEqual(self.state.output()[:2], (24, 24))
        self.assertEqual(self.state.status()["mode"], "seek")
        self.send(v=-.5)
        self.assertEqual(self.state.output()[:2], (-40, -40))
        self.assertEqual(self.state.status()["mode"], "teleop")
        self.send()  # held mode selector must not undo stick override
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.send(deadman=False)
        self.send(mode="explore", target_id=None)
        self.assertEqual(self.state.status()["mode"], "seek")  # person found

    def test_lease_expiry_without_commands_stops_and_requires_release(self):
        self.send()
        self.now += .251
        self.assertEqual(self.state.output()[:2], (0, 0))
        self.assertTrue(self.state.status()["requires_release"])
        self.assertFalse(self.send())

    def test_estop_range_loss_release_and_disconnect_stop_autonomy(self):
        for failure in ("estop", "range_stale", "release", "disconnect"):
            with self.subTest(failure=failure):
                self.setUp()
                self.send()
                self.assertGreater(self.state.output()[0], 0)
                if failure == "estop":
                    self.state.update_range(20)
                elif failure == "range_stale":
                    self.state.update_range(-1)
                elif failure == "release":
                    self.send(deadman=False)
                else:
                    self.state.end(self.session)
                self.assertEqual(self.state.output()[:2], (0, 0))

    def test_old_sensor_stale_video_camera_failure_and_invalid_targets_fail_stopped(self):
        for failure in ("range_stale", "video", "camera", "detector", "target"):
            with self.subTest(failure=failure):
                self.setUp()
                self.send()
                if failure == "range_stale":
                    # Renew operator permits while the front sensor goes silent.
                    self.now += .2
                    self.send(refresh_range=False)
                    self.now += .16
                    self.send(refresh_range=False)
                elif failure == "video":
                    self.assertFalse(self.send(video_fresh=False))
                elif failure == "camera":
                    self.state.camera_ready = lambda: False
                elif failure == "detector":
                    self.state.detections = lambda: 1 / 0
                else:
                    self.assertFalse(self.send(target_id=True))
                self.assertEqual(self.state.output()[:2], (0, 0))


if __name__ == "__main__":
    unittest.main()
