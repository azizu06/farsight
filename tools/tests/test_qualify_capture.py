import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from tools.qualify_capture import Evidence, collect, fetch_health, origin


def healthy(frame=1, **changes):
    h = dict(camera=True, camera_drive_ready=True, camera_capture_verified=True,
        camera_source="PHONE RGB", camera_frame=frame, camera_age_ms=35,
        camera_error=None, control=dict(simulated=False), replay=False,
        map_epoch=1, generation=0, processing_ms=10, detector_inference_ms=15,
        camera_capture_diagnostics=dict(clock="avcapture_sync_to_host", sensor_age_ms=30,
            sensor_host_delta_ms=10, t_capture=100, callback_host_t=100.01,
            sensor_timestamp_raw_t=90, sensor_timestamp_host_t=100))
    h.update(changes)
    return h


class EvidenceTests(unittest.TestCase):
    def test_fresh_view_is_distinct_from_eligible_capture(self):
        e = Evidence()
        sample = e.add(healthy(camera_drive_ready=False, camera_capture_verified=False), 0, 5)
        self.assertTrue(sample["viewing_fresh"])
        self.assertFalse(sample["physical_capture_eligible"])
        e.add(healthy(2), .1, 5)
        self.assertFalse(e.report(.2)["sustained_physical_capture_eligible"], "later recovery cannot erase gate loss")
        qualified = Evidence()
        qualified.add(healthy(), 0, 5); qualified.add(healthy(2), .1, 5)
        self.assertTrue(qualified.report(.2)["sustained_physical_capture_eligible"])

    def test_arkit_plausible_delta_is_never_verification(self):
        h = healthy(camera_source="IPHONE LIDAR")
        h["camera_capture_diagnostics"]["clock"] = "arkit_unverified"
        s = Evidence().add(h, 0, 0)
        self.assertTrue(s["viewing_fresh"])
        self.assertFalse(s["physical_capture_eligible"])
        self.assertIn("unsupported_capture_clock", s["issues"])

    def test_simulation_replay_and_non_boolean_or_missing_gates_fail(self):
        for h in [healthy(control={"simulated": True}), healthy(replay=True),
                  healthy(camera_source="SIMULATED"), healthy(camera_source="REPLAY private-name"),
                  healthy(camera_drive_ready=1), healthy(camera_capture_verified="true"),
                  healthy(control={}), healthy(replay=None), healthy(camera=None)]:
            self.assertFalse(Evidence().add(h, 0, 0)["physical_capture_eligible"])

    def test_repeated_frame_never_renews_age_or_proves_progress(self):
        e = Evidence()
        e.add(healthy(), 0, 5)
        e.add(healthy(), .2, 5)
        self.assertFalse(e.report(.2)["sustained_physical_capture_eligible"])
        s = e.add(healthy(), .5, 5)
        self.assertFalse(s["viewing_fresh"])
        self.assertGreaterEqual(s["camera_receipt_bound_ms"], 540)
        self.assertEqual(e.report(.5)["repeated_frames"], 2)

    def test_resets_are_visible_and_cannot_be_erased_by_later_advances(self):
        for change in [dict(camera_frame=0), dict(map_epoch=2), dict(generation=1),
                       dict(camera_source="USB CAMERA")]:
            e = Evidence(); e.add(healthy(5), 0, 5)
            h = healthy(6); h.update(change); e.add(h, .1, 5)
            h["camera_frame"] += 1; e.add(h, .2, 5)
            report = e.report(.3)
            self.assertEqual(report["identity_resets"], 1)
            self.assertFalse(report["sustained_physical_capture_eligible"])

    def test_bad_numbers_and_negative_sensor_diagnostics_fail_without_raw_leaks(self):
        for field, value in [("camera_age_ms", True), ("camera_frame", 1.0),
                             ("camera_age_ms", float("nan")), ("processing_ms", float("inf")),
                             ("generation", -1)]:
            self.assertFalse(Evidence().add(healthy(**{field: value}), 0, 0)["physical_capture_eligible"])
        for value in [True, -1, 151, "30"]:
            h = healthy(); h["camera_capture_diagnostics"]["sensor_age_ms"] = value
            self.assertFalse(Evidence().add(h, 0, 0)["physical_capture_eligible"])
        h = healthy(session_id="PRIVATE", device="PRIVATE", source="PRIVATE", camera_error="PRIVATE")
        h["control"]["permit"] = "PRIVATE"
        h["camera_capture_diagnostics"]["account"] = "PRIVATE"
        h["camera_source"] = ["PRIVATE"]
        h["camera_capture_diagnostics"]["clock"] = {"account": "PRIVATE"}
        e = Evidence(); e.add(h, 0, 0)
        encoded = json.dumps(e.report(.1), allow_nan=False)
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("session_id", encoded)
        self.assertNotIn('"permit"', encoded)

    def test_keyboard_interrupt_retains_partial_evidence(self):
        count = 0
        def fetch(*args):
            nonlocal count
            count += 1
            if count == 3:
                raise KeyboardInterrupt()
            return healthy(count)
        report = collect("http://127.0.0.1:12345", seconds=.2, interval=.02, fetcher=fetch)
        self.assertTrue(report["interrupted"])
        self.assertEqual(report["samples"], 2)
        self.assertFalse(report["sustained_physical_capture_eligible"])

    def test_configuration_and_origin_bounds(self):
        for url in ["ftp://localhost", "http://user:secret@localhost", "http://localhost/scan",
                    "http://localhost?token=secret", "http://localhost/#secret", "http://localhost:99999"]:
            with self.assertRaises(ValueError): origin(url)
        for changes in [dict(seconds=301), dict(interval=.001), dict(timeout=0),
                        dict(seconds=float('inf')), dict(seconds=300, interval=.02)]:
            with self.assertRaises(ValueError): collect("http://127.0.0.1", **changes)


class LoopbackTests(unittest.TestCase):
    def run_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_real_get_loop_and_interrupted_connection_preserve_failure_after_recovery(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.command, self.path)); count = len(requests)
                if count == 3:
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                if count == 5:
                    time.sleep(.05)  # Last poll has only 1 ms of collection time left.
                body = json.dumps(healthy(count)).encode()
                try:
                    self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
                except OSError:
                    pass  # The bounded client has already closed the final request.
            def log_message(self, *args): pass
        url = self.run_server(Handler)
        clock = SimpleNamespace(now=0.)
        calls = 0
        def fetch(*args):
            nonlocal calls
            calls += 1
            try:
                return fetch_health(*args)  # Keep actual HTTP and header/body deadlines.
            finally:
                if calls == 4:
                    clock.now = .24 - .025 - .001
                elif calls == 5:
                    clock.now = .24
        def sleep(delay):
            clock.now += delay
        # Pin collection scheduling without replacing the real socket I/O.
        with mock.patch('tools.qualify_capture.time',
                        SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)):
            report = collect(url, seconds=.24, interval=.025, timeout=.1, fetcher=fetch)
        self.assertGreater(len(requests), 3)
        self.assertTrue(all(method == 'GET' and path == '/health' for method, path in requests))
        self.assertTrue(any('error' in sample for sample in report['evidence']))
        self.assertGreater(report['advancing_frames'], 0)
        self.assertFalse(report['sustained_physical_capture_eligible'])
        samples = report['evidence']
        interruption = next(index for index, sample in enumerate(samples) if 'error' in sample)
        self.assertTrue(any(sample['physical_capture_eligible'] for sample in samples[interruption + 1:]),
                        'Recovery must be recorded without erasing the interruption')
        # A final request may exhaust the collection deadline after recovery.
        self.assertIn('error', samples[-1])
        self.assertFalse(samples[-1]['physical_capture_eligible'])

    def test_slow_header_trickle_is_bounded_and_redirect_is_not_followed(self):
        visited = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                visited.append(self.path)
                if len(visited) == 1:
                    try:
                        for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}':
                            self.connection.send(bytes([byte])); time.sleep(.02)
                    except OSError: pass
                else:
                    self.send_response(302); self.send_header('Location', '/ws/control'); self.end_headers()
            def log_message(self, *args): pass
        url = self.run_server(Handler)
        started = time.monotonic()
        with self.assertRaises((OSError, ValueError, http.client.HTTPException)): fetch_health(url, .12)
        self.assertLess(time.monotonic() - started, .5)
        report = collect(url, seconds=.1, interval=.02, timeout=.1)
        self.assertFalse(report['sustained_physical_capture_eligible'])
        self.assertNotIn('/ws/control', visited)


if __name__ == '__main__': unittest.main()
