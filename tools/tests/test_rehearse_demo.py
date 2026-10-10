import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

from tools import rehearse_demo as rh

REAL = dict(source="JETSON", camera=True, camera_age_ms=80, camera_source="UVC", detector="trt", detector_backend="tensorrt",
            detector_fps=24.0, detector_error=None, rover=True, estop=False, replay=False,
            video=dict(enabled=True, ready=True, error=None, fps=30, bitrate=2000),
            control=dict(permit_ms=250, rover=True, simulated=False, link_ms=60, left_pwm=0, right_pwm=0,
                         permit="SECRET-PERMIT", session_id="SECRET-SESSION"))


def fake_fetch(health=REAL, fail=()):
    calls = []

    def fetch(base, path, allowed, context=None, timeout=2, headers_only=False):
        calls.append(path)
        if path not in allowed:
            raise ValueError(path)
        if path in fail:
            raise OSError("down")
        if headers_only:
            return {"X-Frame-Age-Ms": "90", "X-Frame-Source": "UVC", "X-Frame-Id": "7"}
        if path == "/control/status":
            return health["control"]
        if path == "/relay/health":
            return {"relay": True, "quest_built": True}
        return health
    fetch.calls = calls
    return fetch


def make_samples(count=1000, step=1.0, start=1000.0, health=REAL):
    samples = []
    for i in range(count):
        s = rh.collect_sample("u", relay="r", fetch=fake_fetch(health))
        s["t"] = start + i * step
        samples.append(s)
    return samples


def states(checks):
    return {c["id"]: c["state"] for c in checks}


def all_marked(state="pass"):
    return {cid: dict(state=state, by="t", note="", at_epoch=1.0) for cid, _ in rh.PHYSICAL}


class CollectTests(unittest.TestCase):
    def test_only_allowlisted_reads_and_secrets_dropped(self):
        fetch = fake_fetch()
        sample = rh.collect_sample("u", relay="r", fetch=fetch)
        self.assertEqual(set(fetch.calls), {"/health", "/control/status", "/camera.jpg", "/relay/health"})
        text = json.dumps(sample)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("permit\"", text)
        self.assertEqual(sample["camera_jpg"]["age_ms"], 90.0)

    def test_passive_get_refuses_other_paths(self):
        for path in ("/ws/control", "/control", "/mission/drill", "/speak"):
            with self.assertRaises(ValueError):
                rh.passive_get("http://127.0.0.1:1", path, rh.ALLOWED_UPSTREAM)

    def test_failures_become_unknown_not_crashes(self):
        sample = rh.collect_sample("u", fetch=fake_fetch(fail=("/health", "/camera.jpg")))
        self.assertNotIn("health", sample)
        self.assertEqual(len(sample["errors"]), 2)

    def test_malformed_health_is_an_error(self):
        sample = rh.collect_sample("u", fetch=lambda *a, **k: ["not", "dict"])
        self.assertIn("health: ValueError", sample["errors"])

    def test_ssh_parse_and_failure(self):
        self.assertEqual(rh.parse_ssh("0.52 0.4 0.3\ncpu-thermal 45000\ngpu-thermal 51500\n"), dict(load1=0.52, temp_c_max=51.5))
        self.assertEqual(rh.parse_ssh("garbage"), dict(error="unparseable"))
        sample = rh.collect_sample("u", ssh="x@y", fetch=fake_fetch(), ssh_run=lambda t: dict(error="ssh exit 255"))
        self.assertEqual(sample["jetson"], dict(error="ssh exit 255"))

    def test_local_server_sees_only_get_of_allowed_paths(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                seen.append(("GET", self.path))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("X-Frame-Age-Ms", "12")
                self.end_headers()
                self.wfile.write(json.dumps(REAL if self.path == "/health" else REAL["control"]).encode())

            def do_POST(self):
                seen.append(("POST", self.path))
                self.send_response(500)
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            sample = rh.collect_sample(f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
        self.assertEqual(sample["errors"], [])
        self.assertEqual({m for m, _ in seen}, {"GET"})
        self.assertEqual({p for _, p in seen}, set(rh.ALLOWED_UPSTREAM))


class LogTests(unittest.TestCase):
    def test_rotation_bounds_retention(self):
        with tempfile.TemporaryDirectory() as d:
            log = rh.BoundedLog(d, max_bytes=500, keep=3)
            for i in range(200):
                log.write(dict(t=1000.0 + i, errors=[], pad="x" * 40))
            log.close()
            files = sorted(p.name for p in Path(d).glob("samples.jsonl*"))
            self.assertEqual(files, ["samples.jsonl", "samples.jsonl.1", "samples.jsonl.2"])
            self.assertLess(sum(p.stat().st_size for p in Path(d).glob("samples.jsonl*")), 3 * 700)
            meta = rh.read_json(Path(d) / "meta.json")
            self.assertEqual(meta["samples"], 200)
            self.assertGreater(meta["rotated_out_bytes"], 0)
            samples, bad = rh.read_samples(d)
            self.assertEqual(bad, 0)
            self.assertEqual(samples[-1]["t"], 1199.0)
            self.assertEqual([s["t"] for s in samples], sorted(s["t"] for s in samples))

    def test_torn_and_malformed_lines_counted(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "samples.jsonl").write_text('{"t":1,"errors":[]}\nnot json\n[1]\n{"t":"x"}\n{"t":2,"err')
            samples, bad = rh.read_samples(d)
            self.assertEqual((len(samples), bad), (1, 4))

    def test_missing_directory_is_empty(self):
        self.assertEqual(rh.read_samples("/nonexistent/rehearsal"), ([], 0))


class EvaluateTests(unittest.TestCase):
    def test_empty_is_unknown_never_go(self):
        checks, summary = rh.evaluate([], {}, {})
        self.assertEqual(states(checks)["samples"], "unknown")
        self.assertNotEqual(summary["verdict"], "GO")

    def test_good_software_without_physical_marks_is_incomplete(self):
        samples = make_samples()
        checks, summary = rh.evaluate(samples, {}, {}, now=samples[-1]["t"] + 1)
        s = states(checks)
        self.assertEqual((s["real_rig"], s["camera_fresh"], s["inference"], s["relay_ack"], s["lease"]), ("pass",) * 5)
        self.assertEqual(s["motor_direction"], "unknown")
        self.assertEqual(summary["verdict"], "INCOMPLETE")

    def test_go_only_when_everything_passes_including_physical(self):
        samples = make_samples()
        checks, summary = rh.evaluate(samples, {}, all_marked(), now=samples[-1]["t"] + 1)
        self.assertEqual(summary["verdict"], "GO", [c for c in checks if c["state"] != "pass"])
        self.assertEqual(summary["max_pwm_seen"], 0)
        partial = {k: v for k, v in all_marked().items() if k != "temperature"}
        self.assertEqual(rh.evaluate(samples, {}, partial, now=samples[-1]["t"] + 1)[1]["verdict"], "INCOMPLETE")

    def test_physical_fail_is_no_go_and_unknown_state_ignored(self):
        samples = make_samples()
        marks = {**all_marked(), "uno_watchdog": dict(state="fail", by="s", note="320 ms", at_epoch=5.0), "temperature": dict(state="bogus")}
        checks, summary = rh.evaluate(samples, {}, marks, now=samples[-1]["t"] + 1)
        s = states(checks)
        self.assertEqual((s["uno_watchdog"], s["temperature"], summary["verdict"]), ("fail", "unknown", "NO-GO"))

    def test_simulation_and_replay_fail_real_rig(self):
        sim = {**REAL, "source": "SIMULATED", "control": {**REAL["control"], "simulated": True}}
        samples = make_samples(health=sim)
        checks, summary = rh.evaluate(samples, {}, all_marked(), now=samples[-1]["t"] + 1)
        self.assertEqual((states(checks)["real_rig"], summary["verdict"]), ("fail", "NO-GO"))
        replay = make_samples(health={**REAL, "replay": True})
        self.assertEqual(states(rh.evaluate(replay, {}, {})[0])["real_rig"], "fail")

    def test_short_run_stale_evidence_and_gaps(self):
        samples = make_samples(count=3, step=5)
        checks, _ = rh.evaluate(samples, {}, {}, now=samples[-1]["t"] + 600)
        s = states(checks)
        self.assertEqual((s["duration"], s["fresh"]), ("fail", "unknown"))
        gappy = make_samples(count=3, step=100)
        self.assertEqual(states(rh.evaluate(gappy, {}, {}, now=gappy[-1]["t"])[0])["coverage"], "fail")

    def test_missing_values_stay_unknown(self):
        bare = {k: v for k, v in REAL.items() if k not in ("camera_age_ms", "detector_fps")}
        bare["control"] = {k: v for k, v in REAL["control"].items() if k != "link_ms"}
        samples = make_samples(health=bare)
        s = states(rh.evaluate(samples, {}, {}, now=samples[-1]["t"])[0])
        self.assertEqual((s["camera_fresh"], s["inference"], s["relay_ack"]), ("unknown",) * 3)

    def test_service_down_all_samples_failing(self):
        down = []
        for i in range(5):
            sample = rh.collect_sample("u", fetch=fake_fetch(fail=("/health", "/control/status", "/camera.jpg")))
            sample["t"] = 1000.0 + i
            down.append(sample)
        checks, summary = rh.evaluate(down, {}, {}, now=1005)
        s = states(checks)
        self.assertEqual((s["reachable"], s["duration"], summary["verdict"]), ("fail", "fail", "NO-GO"))
        self.assertNotIn("real_rig", s)

    def test_slow_link_and_stale_camera_fail(self):
        slow = {**REAL, "camera_age_ms": 900, "control": {**REAL["control"], "link_ms": 400}}
        samples = make_samples(health=slow)
        s = states(rh.evaluate(samples, {}, {}, now=samples[-1]["t"])[0])
        self.assertEqual((s["camera_fresh"], s["relay_ack"]), ("fail", "fail"))

    def test_latency_kinds_kept_separate_and_unknown(self):
        samples = make_samples()
        _, summary = rh.evaluate(samples, {}, {}, now=samples[-1]["t"])
        kinds = summary["latency_kinds"]
        self.assertEqual(set(kinds), {"server_capture_age_ms", "relay_ack_latency_ms", "browser_display_age_ms", "physical_timer_ms"})
        self.assertTrue(kinds["browser_display_age_ms"].startswith("unknown"))
        self.assertTrue(kinds["physical_timer_ms"].startswith("unknown"))


class CommandTests(unittest.TestCase):
    def test_record_interrupt_still_reports_and_stays_bounded(self):
        calls = {"n": 0}

        def sleeper(_):
            calls["n"] += 1
            if calls["n"] == 3:
                raise KeyboardInterrupt

        original = rh.collect_sample
        rh.collect_sample = lambda *a, **k: original("u", fetch=fake_fetch())
        try:
            with tempfile.TemporaryDirectory() as d:
                args = SimpleNamespace(url="u", relay=None, out=d, minutes=1, min_minutes=15, interval=0, max_bytes=10_000,
                                       keep=2, ca_file=None, insecure=False, ssh=None)
                code = rh.record(args, sleep=sleeper)
                self.assertEqual(code, 1)
                for name in ("report.md", "report.html", "report.json", "meta.json"):
                    self.assertTrue((Path(d) / name).exists(), name)
                self.assertEqual(len(rh.read_samples(d)[0]), 3)
                self.assertNotIn("SECRET", (Path(d) / "samples.jsonl").read_text())
        finally:
            rh.collect_sample = original

    def test_mark_then_report_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rh.mark(SimpleNamespace(dir=d, check="motor_direction", state="pass", by="Sai", note="ok")), 0)
            self.assertEqual(rh.mark(SimpleNamespace(dir=d, check="nope", state="pass", by="Sai", note="")), 2)
            samples = make_samples()
            with open(Path(d) / "samples.jsonl", "w") as f:
                f.writelines(json.dumps(s) + "\n" for s in samples)
            checks, summary = rh.build_report(d, now=samples[-1]["t"] + 1)
            self.assertEqual(states(checks)["motor_direction"], "pass")
            self.assertEqual(states(checks)["uno_watchdog"], "unknown")
            self.assertIn("by Sai", next(c for c in checks if c["id"] == "motor_direction")["evidence"])
            self.assertEqual(summary["verdict"], "INCOMPLETE")
            page = (Path(d) / "report.html").read_text()
            self.assertIn("data-theme=\"dark\"", page)
            self.assertIn("INCOMPLETE", (Path(d) / "report.md").read_text() + page)

    def test_every_physical_check_is_listed_in_text_report(self):
        checks, summary = rh.evaluate(make_samples(), {}, {}, now=1e9)
        text = rh.render_text(checks, summary)
        for _, title in rh.PHYSICAL:
            self.assertIn(title, text)
        self.assertIn("Not fully qualified", text)

    def test_rig_day_marks_are_physical_and_unknown_until_marked(self):
        new = {"seek_arrival", "range_stop_25cm", "tailscale_direct", "quest_mic_playback", "wifi_off_run"}
        self.assertLessEqual(new, {cid for cid, _ in rh.PHYSICAL})
        checks, _ = rh.evaluate(make_samples(), {}, {}, now=1e9)
        by_id = {c["id"]: c for c in checks}
        for cid in new:
            self.assertEqual((by_id[cid]["kind"], by_id[cid]["state"]), ("physical", "unknown"))


if __name__ == "__main__":
    unittest.main()
