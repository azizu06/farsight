import json
import tempfile
import threading
import time
import unittest

import cv2
import numpy as np

from jetson.camera import CameraFrame
from jetson.mission import Mission
from jetson.triage import TriageBusy, TriageService, parse_sbar, template_sbar

SBAR_KEYS = ("situation", "background", "assessment", "recommendation")


def frame(frame_id=1, source="PHONE RGB"):
    _, jpeg = cv2.imencode(".jpg", np.zeros((40, 60, 3), np.uint8))
    return CameraFrame(jpeg.tobytes(), time.monotonic(), frame_id, source)


def person(track=3, down=False, conf=.87):
    return dict(id=track, cls="person", conf=conf, x=.4, y=.2, w=.2, h=.6, down=down)


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return False


class TemplateTests(unittest.TestCase):
    def test_down_person_template_prioritises_and_states_facts(self):
        sbar = template_sbar(dict(person(down=True), source="PHONE RGB", first_seen=0, last_seen=5), range_cm=120)
        for key in SBAR_KEYS:
            self.assertIsInstance(sbar[key], str)
            self.assertTrue(sbar[key])
        self.assertEqual(sbar["model"], "template")
        self.assertIn("87%", sbar["situation"])
        self.assertIn("120 cm", sbar["background"])
        self.assertIn("down", sbar["assessment"].lower())
        self.assertIn("priority", sbar["recommendation"].lower())

    def test_upright_and_unknown_pose_and_simulated_are_labelled(self):
        upright = template_sbar(dict(person(down=False), source="USB CAMERA", first_seen=0, last_seen=0))
        self.assertIn("upright", upright["assessment"].lower())
        self.assertIn("unavailable", upright["background"].lower())
        unknown = dict(person(), source="SIMULATED", first_seen=0, last_seen=0)
        unknown.pop("down")
        sbar = template_sbar(unknown)
        self.assertIn("posture unknown", sbar["assessment"].lower())
        self.assertIn("simulated", sbar["situation"].lower())

    def test_parse_sbar_accepts_wrapped_json_and_rejects_partial(self):
        text = 'Sure: {"situation":"S","background":"B","assessment":"A","recommendation":"R"} done'
        self.assertEqual(parse_sbar(text), dict(situation="S", background="B", assessment="A", recommendation="R"))
        with self.assertRaises(ValueError):
            parse_sbar('{"situation":"S"}')
        with self.assertRaises(ValueError):
            parse_sbar("no json here")


class FakeModel:
    def __init__(self, delay=0, fail=False):
        self.calls, self.delay, self.fail = [], delay, fail
        self.release = threading.Event()
        if not delay:
            self.release.set()

    def __call__(self, facts, jpeg):
        self.calls.append((time.monotonic(), facts))
        self.release.wait(5)
        if self.fail:
            raise ValueError("model offline")
        return dict(situation=f"Person {facts['track_id']} seen.", background="B", assessment="A", recommendation="R")


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.mission = Mission(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def service(self, model, interval=0.0, enabled=True):
        service = TriageService(self.mission, server_url="http://127.0.0.1:9/v1" if enabled else None,
                                range_cm=lambda: 80, min_interval=interval, vision=model)
        service.start()
        self.addCleanup(service.close)
        return service

    def test_new_person_gets_template_immediately_then_model_sbar(self):
        model = FakeModel()
        self.service(model)
        self.mission.detect(dict(boxes=[person()]), frame())
        report = self.mission.report()
        entry = report["triage"]["PHONE RGB:3"]
        self.assertEqual(entry["track_id"], 3)
        self.assertTrue(wait_for(lambda: self.mission.report()["triage"]["PHONE RGB:3"]["model"] == "local-vision-model"))
        entry = self.mission.report()["triage"]["PHONE RGB:3"]
        self.assertEqual(entry["situation"], "Person 3 seen.")
        self.assertIsInstance(entry["ms"], int)
        encounter = next(item for item in self.mission.report()["encounters"] if item["key"] == "PHONE RGB:3")
        self.assertEqual(encounter["sbar"]["situation"], "Person 3 seen.")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0][1]["range_cm"], 80)

    def test_sbar_refreshes_when_tracked_person_goes_down(self):
        self.service(FakeModel(), enabled=False)
        self.mission.PERSON_REFRESH_S = 0
        self.mission.detect(dict(boxes=[person(down=False)]), frame(1))
        self.assertIn("upright", self.mission.report()["triage"]["PHONE RGB:3"]["assessment"])
        self.mission.detect(dict(boxes=[person(down=False)]), frame(2))
        self.mission.detect(dict(boxes=[person(down=True)]), frame(3))
        entry = self.mission.report()["triage"]["PHONE RGB:3"]
        self.assertIn("down", entry["assessment"].lower())
        self.assertIs(entry["down"], True)

    def test_down_change_inside_rate_limit_window_is_applied_once_the_window_passes(self):
        self.service(FakeModel(), enabled=False)
        self.mission.PERSON_REFRESH_S = 60
        self.mission.detect(dict(boxes=[person(down=False)]), frame(1))
        self.mission.detect(dict(boxes=[person(down=True)]), frame(2))
        self.assertIn("upright", self.mission.report()["triage"]["PHONE RGB:3"]["assessment"])
        self.mission.PERSON_REFRESH_S = 0
        self.mission.detect(dict(boxes=[person(down=True)]), frame(3))
        self.assertIn("down", self.mission.report()["triage"]["PHONE RGB:3"]["assessment"].lower())

    def test_non_person_and_repeat_frames_do_not_trigger_triage(self):
        model = FakeModel()
        self.service(model)
        self.mission.detect(dict(boxes=[dict(id=1, cls="chair", conf=.9, x=0, y=0, w=.1, h=.1)]), frame(1))
        self.mission.detect(dict(boxes=[person()]), frame(2))
        self.mission.detect(dict(boxes=[person()]), frame(3))
        self.assertTrue(wait_for(lambda: len(model.calls) == 1))
        time.sleep(.05)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(list(self.mission.report()["triage"]), ["PHONE RGB:3"])

    def test_detection_never_waits_for_slow_model_and_calls_are_rate_limited(self):
        model = FakeModel(delay=True)
        self.service(model, interval=.2)
        started = time.monotonic()
        for track in range(1, 4):
            self.mission.detect(dict(boxes=[person(track)]), frame(track))
        self.assertLess(time.monotonic() - started, .1)  # detector thread is not blocked by the VLM
        self.assertEqual(len(self.mission.report()["triage"]), 3)  # every person already has a template
        model.release.set()
        self.assertTrue(wait_for(lambda: len(model.calls) == 3))
        gaps = [later[0] - earlier[0] for earlier, later in zip(model.calls, model.calls[1:])]
        self.assertTrue(all(gap >= .19 for gap in gaps), gaps)

    def test_model_failure_keeps_template_with_warning(self):
        model = FakeModel(fail=True)
        self.service(model)
        self.mission.detect(dict(boxes=[person(down=True)]), frame())
        self.assertTrue(wait_for(lambda: "warning" in self.mission.report()["triage"]["PHONE RGB:3"]))
        entry = self.mission.report()["triage"]["PHONE RGB:3"]
        self.assertEqual(entry["model"], "template")
        self.assertIn("down", entry["assessment"].lower())

    def test_without_model_url_template_only(self):
        model = FakeModel()
        self.service(model, enabled=False)
        self.mission.detect(dict(boxes=[person()]), frame())
        time.sleep(.05)
        self.assertEqual(model.calls, [])
        self.assertEqual(self.mission.report()["triage"]["PHONE RGB:3"]["model"], "template")

    def test_sbar_is_persisted_in_mission_report_file(self):
        self.service(FakeModel())
        self.mission.detect(dict(boxes=[person()]), frame())
        path = self.mission.directory / "report.json"
        self.assertTrue(wait_for(lambda: path.is_file() and json.loads(path.read_text())["triage"]
                                 .get("PHONE RGB:3", {}).get("model") == "local-vision-model"))
        saved = json.loads(path.read_text())
        self.assertEqual(saved["mission_id"], self.mission.id)
        self.assertTrue(saved["triage"]["PHONE RGB:3"]["snapshot_url"].startswith("/mission/snapshots/detection-"))
        lines = (self.mission.directory / "triage.jsonl").read_text().splitlines()
        self.assertGreaterEqual(len(lines), 2)  # template then model SBAR

    def test_on_demand_generate_refreshes_and_reports_busy(self):
        model = FakeModel()
        service = self.service(model)
        self.mission.detect(dict(boxes=[person()]), frame())
        self.assertTrue(wait_for(lambda: len(model.calls) == 1))
        sbar = service.generate("PHONE RGB:3")
        self.assertEqual(sbar["model"], "local-vision-model")
        self.assertEqual(len(model.calls), 2)
        with self.assertRaises(KeyError):
            service.generate("PHONE RGB:99")
        slow = FakeModel(delay=True)
        service.vision = slow
        worker = threading.Thread(target=service.generate, args=("PHONE RGB:3",))
        worker.start()
        self.assertTrue(wait_for(lambda: len(slow.calls) == 1))
        with self.assertRaises(TriageBusy):
            service.generate("PHONE RGB:3")
        slow.release.set()
        worker.join(2)


if __name__ == "__main__":
    unittest.main()
