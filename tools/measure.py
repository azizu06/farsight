"""Bring-up measurements against a running rover server. Stdlib only: run on the Jetson or the Mac.

python -m tools.measure stream   --url https://farsight.local:8443 --ca-file ca.pem    # issue #1: MJPEG fps
python -m tools.measure detector --url http://127.0.0.1:8443                           # issue #9: fps + ms/frame
python -m tools.measure ask      --url http://127.0.0.1:8443 --runs 5                  # issue #19: per-answer latency

Every subcommand prints a table and one JSON line (prefixed RESULT) to paste into a PR. Run `detector`
with a person in view: with no detections the loop idles and reports no samples.
"""

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request

from .netutil import fetch_json, percentile, tls_context


class MeasureError(RuntimeError):
    pass


class MjpegParser:
    """Incremental multipart/x-mixed-replace splitter. A frame counts when the next boundary (or finish) arrives."""

    def __init__(self, boundary=b"--frame"):
        self.boundary = boundary
        self.buffer = b""
        self.frames = []  # (monotonic arrival of the frame's boundary, JPEG bytes)
        self._started = None

    def feed(self, chunk, now=time.monotonic):
        self.buffer += chunk
        while True:
            at = self.buffer.find(self.boundary)
            if at < 0:
                if self._started is None:  # preamble: keep only a possible split boundary
                    self.buffer = self.buffer[-len(self.boundary):]
                return
            if self._started is not None:
                self._complete(self.buffer[:at], self._started)
            self._started = now()
            self.buffer = self.buffer[at + len(self.boundary):]

    def finish(self):
        # The last part has no closing boundary; keep it only if it is a whole JPEG.
        if self._started is not None:
            self._complete(self.buffer, self._started)
            self._started = None

    def _complete(self, part, stamp):
        _, _, body = part.partition(b"\r\n\r\n")
        body = body[:-2] if body.endswith(b"\r\n") else body
        if body.startswith(b"\xff\xd8") and body.endswith(b"\xff\xd9"):
            self.frames.append((stamp, len(body)))


def summarize_stream(frames, seconds):
    stamps = [stamp for stamp, _ in frames]
    gaps = [(b - a) * 1000 for a, b in zip(stamps, stamps[1:])]
    span = (stamps[-1] - stamps[0]) if len(stamps) > 1 else 0
    fps = (len(stamps) - 1) / span if span > 0 else 0
    return dict(frames=len(frames), seconds=round(seconds, 2), fps=round(fps, 1),
                interval_ms_median=round(statistics.median(gaps), 1) if gaps else None,
                interval_ms_p95=round(percentile(gaps, .95), 1) if gaps else None,
                interval_ms_max=round(max(gaps), 1) if gaps else None,
                kb_per_frame=round(statistics.mean(size for _, size in frames) / 1000, 1) if frames else None,
                mbit_per_s=round(sum(size for _, size in frames) * 8 / 1e6 / seconds, 2) if seconds > 0 else 0)


def summarize_detector(samples):
    live = [s for s in samples if s.get("detector_fps")]
    if not live:
        return dict(samples=0, fps_median=0, backend=None)
    latency = [s["detector_inference_ms"] for s in live if s.get("detector_inference_ms") is not None]
    return dict(samples=len(live), backend=live[-1].get("detector_backend") or live[-1].get("detector"),
                fps_median=statistics.median(s["detector_fps"] for s in live),
                fps_min=min(s["detector_fps"] for s in live),
                inference_ms_median=statistics.median(latency) if latency else None,
                inference_ms_p95=percentile(latency, .95), inference_ms_max=max(latency) if latency else None,
                errors=sorted({s["detector_error"] for s in samples if s.get("detector_error")}))


def summarize_ask(runs):
    """First run is reported as cold (model load, cache fill); the rest are warm."""
    totals = [r["ms"] for r in runs]
    warm = runs[1:] or runs
    stages = {}
    for name in ("stt", "vlm", "tts"):
        values = [r["timings"][name] for r in warm if (r.get("timings") or {}).get(name) is not None]
        if values:
            stages[name] = dict(median=statistics.median(values), max=max(values))
    warm_totals = [r["ms"] for r in warm]
    return dict(runs=len(runs), cold_ms=totals[0], warm_total_ms_median=statistics.median(warm_totals),
                warm_total_ms_p95=percentile(warm_totals, .95), warm_total_ms_max=max(warm_totals), stages=stages)


def _open(request, context, timeout):
    try:
        return urllib.request.urlopen(request, timeout=timeout, context=context)
    except (OSError, urllib.error.URLError) as exc:
        raise MeasureError(f"{request if isinstance(request, str) else request.full_url}: {exc}") from exc


def measure_stream(url, seconds=10, context=None, path="/stream.mjpg", out=sys.stdout):
    parser = MjpegParser()
    started = time.monotonic()
    with _open(url.rstrip("/") + path, context, timeout=5) as response:
        while time.monotonic() - started < seconds:
            chunk = response.read1(65536) if hasattr(response, "read1") else response.read(65536)
            if not chunk:
                break
            parser.feed(chunk)
    parser.finish()
    result = summarize_stream(parser.frames, time.monotonic() - started)
    try:
        health = fetch_json(url, "/health", context)
        result.update(camera=health.get("camera"), camera_source=health.get("camera_source"),
                      camera_error=health.get("camera_error"))
    except (OSError, ValueError):
        pass
    print(f"stream {path}: {result['fps']} fps over {result['seconds']}s ({result['frames']} frames), "
          f"gap median/p95/max {result['interval_ms_median']}/{result['interval_ms_p95']}/{result['interval_ms_max']} ms, "
          f"{result['kb_per_frame']} kB/frame = {result['mbit_per_s']} Mbit/s", file=out)
    return result


def measure_detector(url, seconds=15, interval=0.5, context=None, out=sys.stdout):
    samples, deadline = [], time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            samples.append(fetch_json(url, "/health", context))
        except (OSError, ValueError) as exc:
            raise MeasureError(f"{url}/health: {exc}") from exc
        time.sleep(interval)
    result = summarize_detector(samples)
    print(f"detector {result.get('backend')}: median {result.get('fps_median')} fps "
          f"(min {result.get('fps_min')}), {result.get('inference_ms_median')} ms/frame "
          f"(p95 {result.get('inference_ms_p95')}, max {result.get('inference_ms_max')}) from {result['samples']} samples", file=out)
    if result["samples"] == 0:
        print("  no live detections: put a person in view, or read detector_error from /health", file=out)
    return result


def measure_ask(url, text, runs=5, audio=None, context=None, out=sys.stdout):
    results = []
    for i in range(runs):
        if audio:
            boundary = "----farsight-measure"
            data = open(audio, "rb").read()
            body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"q.wav\"\r\n"
                    f"Content-Type: audio/wav\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
            headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        else:
            body, headers = json.dumps({"text": text}).encode(), {"Content-Type": "application/json"}
        request = urllib.request.Request(url.rstrip("/") + "/ask", data=body, headers=headers, method="POST")
        started = time.monotonic()
        with _open(request, context, timeout=60) as response:
            answer = json.load(response)
        answer["wall_ms"] = round((time.monotonic() - started) * 1000)
        answer.setdefault("ms", answer["wall_ms"])  # servers without the server-side total (Rust port) still measure
        results.append(answer)
        print(f"  run {i + 1}: {answer.get('ms')} ms server, {answer['wall_ms']} ms wall, timings {answer.get('timings')}, "
              f"model {answer.get('model')}: {str(answer.get('answer'))[:70]}", file=out)
    result = summarize_ask(results)
    print(f"ask: cold {result['cold_ms']} ms; warm median {result['warm_total_ms_median']} ms "
          f"(p95 {result['warm_total_ms_p95']}, max {result['warm_total_ms_max']}); stages {result['stages']}", file=out)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("stream", "detector", "ask"):
        p = sub.add_parser(name)
        p.add_argument("--url", default="http://127.0.0.1:8443")
        p.add_argument("--ca-file")
        p.add_argument("--insecure", action="store_true")
        if name != "ask":
            p.add_argument("--seconds", type=float, default=10 if name == "stream" else 15)
    sub.choices["ask"].add_argument("--runs", type=int, default=5)
    sub.choices["ask"].add_argument("--text", default="What do you see?")
    sub.choices["ask"].add_argument("--audio", help="WAV/WebM question to include speech-to-text in the timing")
    args = parser.parse_args(argv)
    context = tls_context(args.ca_file, args.insecure)
    try:
        if args.command == "stream":
            result = measure_stream(args.url, args.seconds, context)
        elif args.command == "detector":
            result = measure_detector(args.url, args.seconds, context=context)
        else:
            result = measure_ask(args.url, args.text, args.runs, args.audio, context)
    except MeasureError as exc:
        sys.exit(f"measure: {exc}")
    print("RESULT " + json.dumps(dict(command=args.command, **result)))


if __name__ == "__main__":
    main()
