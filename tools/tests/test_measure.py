import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO

from tools import measure

JPEG = b"\xff\xd8" + b"\x00" * 98 + b"\xff\xd9"


def part(body=JPEG):
    return b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + body + b"\r\n"


class MjpegParserTests(unittest.TestCase):
    def test_counts_complete_frames_across_arbitrary_chunking(self):
        data = part() * 4
        parser = measure.MjpegParser()
        stamps = iter(range(100))
        for i in range(0, len(data), 7):
            parser.feed(data[i:i + 7], now=lambda: next(stamps))
        self.assertEqual(len(parser.frames), 3)  # the 4th is only complete once the next boundary arrives
        parser.finish()
        self.assertEqual(len(parser.frames), 4)
        self.assertTrue(all(size == len(JPEG) for _, size in parser.frames))

    def test_ignores_non_jpeg_parts(self):
        parser = measure.MjpegParser()
        parser.feed(part(b"not a jpeg") + part(), now=lambda: 1.0)
        parser.finish()
        self.assertEqual(len(parser.frames), 1)


class SummaryTests(unittest.TestCase):
    def test_stream_summary(self):
        frames = [(i * 0.1, 2000) for i in range(11)]
        s = measure.summarize_stream(frames, seconds=1.0)
        self.assertAlmostEqual(s["fps"], 10.0, places=1)
        self.assertAlmostEqual(s["interval_ms_p95"], 100, delta=1)
        self.assertEqual(s["kb_per_frame"], 2.0)
        self.assertEqual(s["frames"], 11)

    def test_stream_summary_with_no_frames(self):
        self.assertEqual(measure.summarize_stream([], seconds=5)["fps"], 0)

    def test_detector_summary_ignores_idle_samples(self):
        samples = [dict(detector_fps=0, detector_inference_ms=None),
                   dict(detector_fps=14.0, detector_inference_ms=20.0, detector_backend="pose-tensorrt"),
                   dict(detector_fps=16.0, detector_inference_ms=30.0, detector_backend="pose-tensorrt")]
        s = measure.summarize_detector(samples)
        self.assertEqual(s["fps_median"], 15.0)
        self.assertEqual(s["inference_ms_max"], 30.0)
        self.assertEqual(s["backend"], "pose-tensorrt")
        self.assertEqual(s["samples"], 2)

    def test_ask_summary_reports_each_stage_and_cold_run(self):
        runs = [dict(ms=2400, timings=dict(stt=None, vlm=2300, tts=60)),
                dict(ms=800, timings=dict(stt=None, vlm=700, tts=50)),
                dict(ms=900, timings=dict(stt=None, vlm=800, tts=55))]
        s = measure.summarize_ask(runs)
        self.assertEqual(s["cold_ms"], 2400)
        self.assertEqual(s["warm_total_ms_median"], 850)
        self.assertEqual(s["stages"]["vlm"]["median"], 750)
        self.assertNotIn("stt", s["stages"])


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/stream.mjpg":
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(part())
                    self.wfile.flush()
                    threading.Event().wait(0.01)
            except OSError:
                pass
        elif self.path == "/health":
            body = json.dumps(dict(camera=True, camera_source="USB CAMERA", detector="pose",
                                   detector_backend="pose-tensorrt", detector_fps=15.0,
                                   detector_inference_ms=22.0)).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        body = json.dumps(dict(answer="ok", ms=800, timings=dict(stt=None, vlm=700, tts=50))).encode()
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class LiveTests(unittest.TestCase):
    def setUp(self):
        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.url = f"http://127.0.0.1:{server.server_port}"

    def test_stream_measures_a_live_mjpeg(self):
        out = StringIO()
        result = measure.measure_stream(self.url, seconds=0.5, out=out)
        self.assertGreater(result["frames"], 5)
        self.assertGreater(result["fps"], 5)
        self.assertIn("fps", out.getvalue())

    def test_detector_polls_health(self):
        result = measure.measure_detector(self.url, seconds=0.4, interval=0.05, out=StringIO())
        self.assertEqual(result["backend"], "pose-tensorrt")
        self.assertEqual(result["fps_median"], 15.0)

    def test_ask_posts_text_questions(self):
        result = measure.measure_ask(self.url, "what do you see?", runs=3, out=StringIO())
        self.assertEqual(result["runs"], 3)
        self.assertEqual(result["warm_total_ms_median"], 800)

    def test_unreachable_server_raises_readable_error(self):
        with self.assertRaises(measure.MeasureError):
            measure.measure_stream("http://127.0.0.1:9", seconds=0.2, out=StringIO())


if __name__ == "__main__":
    unittest.main()
