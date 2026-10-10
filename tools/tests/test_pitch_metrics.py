import asyncio
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

from tools import pitch_metrics

ROOT = Path(__file__).resolve().parents[2]


class MathTests(unittest.TestCase):
    def test_stats_nearest_rank(self):
        s = pitch_metrics.stats(list(range(1, 101)))
        self.assertEqual((s["n"], s["p50"], s["p95"], s["max"]), (100, 50.5, 95, 100))

    def test_stats_empty_is_none_not_zero(self):
        self.assertEqual(pitch_metrics.stats([]), dict(n=0, p50=None, p95=None, max=None))

    def test_segments_fps_uses_arrival_span_and_skips_first_segment(self):
        # 1 frame every 0.1 s for 1 s: 10 intervals, 10 frames after the first.
        segments = [(i * 0.1, 1, 5000, 100 + i) for i in range(11)]
        s = pitch_metrics.summarize_segments(segments, seconds=1.0)
        self.assertAlmostEqual(s["fps"], 10.0, places=1)
        self.assertAlmostEqual(s["mbit_per_s"], 0.44, places=2)
        self.assertEqual(s["server_age_ms"]["p50"], 105)

    def test_segments_multi_frame_and_degenerate(self):
        self.assertAlmostEqual(pitch_metrics.summarize_segments(
            [(0, 2, 1, None), (0.5, 2, 1, None), (1.0, 2, 1, None)], 1.0)["fps"], 4.0)
        self.assertEqual(pitch_metrics.summarize_segments([(0, 1, 1, None)], 1.0)["fps"], 0)

    def test_source_label_marks_synthetic_inputs(self):
        self.assertEqual(pitch_metrics.source_label({"camera_source": "SIMULATED"}), "simulated")
        self.assertEqual(pitch_metrics.source_label({"camera_source": "USB", "control": {"simulated": True}}), "simulated")
        self.assertEqual(pitch_metrics.source_label({"camera_source": "USB", "control": {"simulated": False}}), "rig")

    def test_markdown_rig_only_rows_stay_pending(self):
        report = dict(label="simulated", seconds_per_phase=10,
                      detector=dict(samples=4, backend="hog", fps_median=3.0),
                      stream=dict(mjpeg=dict(fps=14.6), h264=dict(skipped="no")),
                      control=dict(rtt_ms=pitch_metrics.stats([10, 20, 30], 2),
                                   permit_age_at_send_ms=pitch_metrics.stats([5, 6])))
        table = pitch_metrics.render_markdown(report)
        self.assertIn("| Detector fps | 3.0 fps |", table)
        self.assertIn("| Stream fps, MJPEG | 14.6 fps |", table)
        self.assertIn("| Stream fps, H.264 | pending rig run |", table)  # skipped phase is not a number
        self.assertIn("| Control round-trip, p50 / p95 | 20 / 30 ms |", table)
        self.assertIn("| Glass-to-glass latency | pending rig run |", table)
        self.assertIn("simulated (laptop)", table)


class MockServerRun(unittest.IsolatedAsyncioTestCase):
    """One real run against `jetson.server --demo --simulate-rover` (no motors, no camera)."""

    async def asyncSetUp(self):
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            self.port = reserved.getsockname()[1]
        self.h264 = shutil.which("ffmpeg") is not None
        command = [sys.executable, "-m", "jetson.server", "--demo", "--simulate-rover",
                   "--host", "127.0.0.1", "--port", str(self.port)] + (["--h264"] if self.h264 else [])
        self.process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)
        self.origin = f"http://127.0.0.1:{self.port}"
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            self.assertIsNone(self.process.poll(), "mock server exited")
            try:
                with urllib.request.urlopen(self.origin + "/health", timeout=.5) as response:
                    if response.status == 200:
                        return
            except OSError:
                await asyncio.sleep(.2)
        self.fail("mock server did not start")

    async def asyncTearDown(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()

    async def test_default_run_is_read_only_and_reports_labelled_numbers(self):
        report = await pitch_metrics.collect(self.origin, seconds=3, hold_permit=False)
        self.assertEqual(report["label"], "simulated")
        self.assertTrue(report["read_only"])
        self.assertFalse(report["motion_requested"])
        self.assertEqual(report["control"]["errors"], [])
        self.assertGreater(report["control"]["rtt_ms"]["n"], 10)
        self.assertGreater(report["control"]["permit_age_at_send_ms"]["n"], 10)
        self.assertEqual(report["control"]["server_link_ms"]["n"], 0)  # no held deadman, so no server link_ms
        self.assertGreater(report["stream"]["mjpeg"]["fps"], 2)
        if self.h264:
            self.assertGreater(report["stream"]["h264"]["fps"], 2)
        self.assertGreater(report["detector"]["samples"], 0)
        # The rover never saw a held deadman: still stopped, no permit left behind.
        with urllib.request.urlopen(self.origin + "/control/status", timeout=2) as response:
            status = json.load(response)
        self.assertEqual((status["left_pwm"], status["right_pwm"]), (0, 0))
        table = pitch_metrics.render_markdown(report)
        self.assertIn("simulated (laptop)", table)
        self.assertIn("| Stop distance after deadman release | pending rig run |", table)

    async def test_hold_permit_reads_server_link_ms_without_motion(self):
        report = await pitch_metrics.collect(self.origin, seconds=2, hold_permit=True, video=False)
        self.assertFalse(report["read_only"])
        self.assertFalse(report["motion_requested"])
        self.assertGreater(report["control"]["server_link_ms"]["n"], 5)
        self.assertLess(report["control"]["server_link_ms"]["p95"], 250)
        with urllib.request.urlopen(self.origin + "/control/status", timeout=2) as response:
            status = json.load(response)
        self.assertEqual((status["left_pwm"], status["right_pwm"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
