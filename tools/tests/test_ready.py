import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO

from tools import ready

OK = dict(camera=True, camera_error=None, detector="hog", detector_error=None, detector_fps=3.0,
          rover=True, control=dict(simulated=False), video=dict(enabled=True, ready=True, error=None))


def health(**changes):
    return {**OK, **changes}


class EvaluateTests(unittest.TestCase):
    def states(self, h):
        return {name: state for name, state, _ in ready.evaluate(h)}

    def test_all_ok(self):
        self.assertEqual(self.states(OK), dict(camera="ok", detector="ok", rover="ok", video="ok"))

    def test_waiting_and_failing(self):
        s = self.states(health(camera=False, rover=False, detector_fps=0,
                               video=dict(enabled=True, ready=False, error="ffmpeg missing")))
        self.assertEqual(s, dict(camera="wait", detector="wait", rover="wait", video="fail"))

    def test_disabled_subsystems_are_skipped(self):
        s = self.states(health(detector="none", video=dict(enabled=False, ready=False, error=None)))
        self.assertEqual((s["detector"], s["video"]), ("skip", "skip"))

    def test_detector_error_fails(self):
        self.assertEqual(self.states(health(detector_error="engine missing"))["detector"], "fail")

    def test_simulated_rover_is_labelled(self):
        detail = dict((n, d) for n, _, d in ready.evaluate(health(control=dict(simulated=True))))["rover"]
        self.assertIn("simulated", detail)

    def test_skip_marks_named_subsystems_skipped(self):
        rows = ready.evaluate(health(camera=True, rover=False, video=dict(enabled=True, ready=False, error=None)),
                              skip=("rover", "video"))
        states = {name: state for name, state, _ in rows}
        self.assertEqual((states["rover"], states["video"]), ("skip", "skip"))
        self.assertTrue(ready.is_ready(rows))

    def test_detector_detail_shows_backend_and_latency(self):
        detail = dict((n, d) for n, _, d in ready.evaluate(
            health(detector="pose", detector_backend="pose-tensorrt", detector_inference_ms=21.4)))["detector"]
        self.assertIn("pose-tensorrt", detail)
        self.assertIn("21.4 ms", detail)

    def test_require_tensorrt_fails_cpu_backends(self):
        cpu = health(detector="pose", detector_backend="pose-onnx")
        gpu = health(detector="pose", detector_backend="pose-tensorrt")
        self.assertEqual(dict((n, s) for n, s, _ in ready.evaluate(cpu, require_tensorrt=True))["detector"], "fail")
        self.assertEqual(dict((n, s) for n, s, _ in ready.evaluate(gpu, require_tensorrt=True))["detector"], "ok")
        self.assertEqual(dict((n, s) for n, s, _ in ready.evaluate(cpu))["detector"], "ok")

    def test_assistant_row_is_informational(self):
        on = dict((n, (s, d)) for n, s, d in ready.evaluate(
            health(assistant=dict(vision_model=True, whisper=True, piper=False))))["assistant"]
        self.assertEqual(on[0], "ok")
        self.assertIn("vision", on[1])
        self.assertIn("no piper", on[1])
        off = dict((n, s) for n, s, _ in ready.evaluate(
            health(assistant=dict(vision_model=False, whisper=False, piper=False))))["assistant"]
        self.assertEqual(off, "skip")

    def test_is_ready_ignores_skips_only(self):
        self.assertTrue(ready.is_ready(ready.evaluate(health(detector="none"))))
        self.assertFalse(ready.is_ready(ready.evaluate(health(camera=False))))


class Handler(BaseHTTPRequestHandler):
    bodies = []

    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        body = json.dumps(self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ProbeTests(unittest.TestCase):
    def serve(self, bodies):
        Handler.bodies = list(bodies)
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}"

    def test_polls_until_ready_and_prints_total(self):
        url = self.serve([health(camera=False), OK])
        out = StringIO()
        code = ready.probe(url, timeout=5, interval=0.01, boot_seconds=lambda: 41.0, out=out)
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("camera", text)
        self.assertIn("READY", text)
        self.assertIn("since boot", text)

    def test_times_out_nonzero_when_server_down(self):
        out = StringIO()
        code = ready.probe("http://127.0.0.1:9", timeout=0.3, interval=0.05, boot_seconds=lambda: None, out=out)
        self.assertEqual(code, 1)
        self.assertIn("server", out.getvalue())
        self.assertIn("NOT READY", out.getvalue())


if __name__ == "__main__":
    unittest.main()
