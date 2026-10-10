import contextlib
import importlib.util
import io
import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from tools import rig_day

ARGS = ["--url", "http://192.168.55.1:8443", "--relay", "http://127.0.0.1:8080"]


def run_main(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = rig_day.main(list(argv))
    return code, out.getvalue()


class DryRunTests(unittest.TestCase):
    def test_dry_run_prints_exact_commands_in_order_and_runs_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            code, text = run_main(*ARGS, "--out", d, "--dry-run")
            self.assertEqual(code, 0)
            self.assertEqual(list(Path(d).iterdir()), [])
        lines = text.splitlines()
        self.assertEqual([line.split(":")[0] for line in lines], list(rig_day.STEPS))
        py = sys.executable
        self.assertIn(f"{py} -m tools.ready --url http://192.168.55.1:8443", lines[1])
        self.assertIn("-m tools.measure stream --url http://192.168.55.1:8443", lines[2])
        self.assertIn("-m tools.measure detector --url http://192.168.55.1:8443", lines[3])
        self.assertIn("-m tools.qualify_link --url http://127.0.0.1:8080", lines[4])
        self.assertIn("-m tools.rehearse_demo record --url http://192.168.55.1:8443 --relay http://127.0.0.1:8080", lines[6])

    def test_default_path_never_drives_or_holds_a_permit(self):
        _, text = run_main(*ARGS, "--out", "/tmp/unused", "--dry-run")
        self.assertNotIn("--hold-permit", text)

    def test_skip_is_marked_in_dry_run(self):
        _, text = run_main(*ARGS, "--out", "/tmp/unused", "--dry-run", "--skip", "ready", "--skip", "preflight")
        skipped = [line.split(":")[0] for line in text.splitlines() if line.startswith("SKIPPED")]
        self.assertEqual(skipped, ["SKIPPED preflight", "SKIPPED ready"])


class StepTests(unittest.TestCase):
    def args(self, **extra):
        base = dict(skip=[], ca_file=None, insecure=False, probe_status_url="http://x", url="http://x", relay=None, out="")
        return SimpleNamespace(**{**base, **extra})

    def test_control_probes_refused_when_owner_connected_or_unverifiable(self):
        with tempfile.TemporaryDirectory() as d:
            ran = []
            runner = lambda *a, **k: ran.append(a) or SimpleNamespace(returncode=0)
            for step in rig_day.CONTROL_PROBES:
                state, detail = rig_day.run_step(step, ["x"], Path(d), self.args(), runner, lambda *_: "another control socket owner is connected")
                self.assertEqual(state, "FAIL")
                self.assertIn("another control socket owner", detail)
            self.assertEqual(ran, [])

    def test_owner_check_reads_session_id(self):
        self.assertIsNone(rig_day.control_owner("http://x", fetch=lambda *a: {"session_id": None}))
        self.assertIn("owner", rig_day.control_owner("http://x", fetch=lambda *a: {"session_id": "abc"}))

        def down(*_):
            raise OSError("down")
        self.assertIn("cannot verify", rig_day.control_owner("http://x", fetch=down))

    def test_skip_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(rig_day.run_step("ready", ["x"], Path(d), self.args(skip=["ready"]))[0], "SKIPPED")
            fail = lambda *a, **k: SimpleNamespace(returncode=3)
            self.assertEqual(rig_day.run_step("ready", ["x"], Path(d), self.args(), fail)[0], "FAIL")
            ok = lambda *a, **k: SimpleNamespace(returncode=0)
            self.assertEqual(rig_day.run_step("ready", ["x"], Path(d), self.args(), ok)[0], "PASS")
            self.assertEqual(rig_day.run_step("qualify_link", ["x"], Path(d), self.args(), ok, lambda *_: None)[0], "PASS")


@unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("aiohttp"), "needs server deps")
class FixtureRunTests(unittest.TestCase):
    def test_simulated_rover_is_no_go_with_new_marks_unknown(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = subprocess.Popen([sys.executable, "-m", "jetson.server", "--demo", "--simulate-rover", "--port", str(port)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(60):
                try:
                    urllib.request.urlopen(url + "/health", timeout=1)
                    break
                except OSError:
                    time.sleep(0.5)
            with tempfile.TemporaryDirectory() as d:
                code, _ = run_main("--url", url, "--out", d, "--seconds", "2", "--rehearse-minutes", "0.05",
                                   "--ready-timeout", "10", "--skip", "preflight")
                self.assertEqual(code, 1)
                summary = (Path(d) / "summary.md").read_text()
                self.assertIn("| preflight | SKIPPED |", summary)
                self.assertIn("verdict NO-GO", summary)
                report = json.loads((Path(d) / "rehearsal" / "report.json").read_text())
                self.assertEqual(report["summary"]["verdict"], "NO-GO")
                states = {c["id"]: c["state"] for c in report["checks"]}
                for cid in ("seek_arrival", "range_stop_25cm", "tailscale_direct", "quest_mic_playback", "wifi_off_run"):
                    self.assertEqual(states[cid], "unknown")
                self.assertEqual(states["real_rig"], "fail")
        finally:
            server.terminate()
            server.wait(10)


if __name__ == "__main__":
    unittest.main()
