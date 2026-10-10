"""GET-only sustained capture evidence; never opens a producer or control socket."""

import argparse
import json
import http.client
import math
from pathlib import Path
import socket
import statistics
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request

from .netutil import percentile, tls_context

CAMERA_MAX_AGE_MS = 500
SENSOR_MAX_AGE_MS = 150
MAX_BODY = 256 * 1024
MAX_SAMPLES = 2000
SOURCES = {"PHONE RGB", "IPHONE LIDAR", "USB CAMERA", "SIMULATED", "WAITING"}
CLOCKS = {"avcapture_sync_to_host", "arkit_unverified"}
NUMBERS = ("camera_age_ms", "detector_inference_ms", "processing_ms", "detector_fps")
DIAGNOSTICS = ("t_capture", "callback_host_t", "sensor_timestamp_raw_t",
               "sensor_timestamp_host_t", "sensor_host_delta_ms", "sensor_age_ms")


def origin(value):
    """A credential-free origin, with no routes, query strings or fragments."""
    p = urllib.parse.urlsplit(value)
    if (p.scheme not in ("http", "https") or not p.hostname or p.username is not None
            or p.password is not None or p.path not in ("", "/") or p.query or p.fragment
            or len(value) > 2048 or any(c.isspace() for c in value)):
        raise ValueError("Use a credential-free HTTP(S) origin")
    _ = p.port  # validate numeric/range syntax
    return urllib.parse.urlunsplit((p.scheme, p.netloc, "", "", ""))


class HeaderDeadline:
    def getresponse(self):
        # Socket timeouts alone do not stop a peer trickling one header byte
        # per timeout period. Bound header parsing too, without a worker pool.
        sock = self.sock
        def abort():
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(self.timeout, abort)
        timer.daemon = True
        timer.start()
        try:
            response = super().getresponse()
            if response.status == 200:
                response._capture_timer = timer
                return response
            timer.cancel(); timer.join()
            return response
        except BaseException:
            timer.cancel(); timer.join()
            raise


class HTTPConnection(HeaderDeadline, http.client.HTTPConnection):
    pass


class HTTPSConnection(HeaderDeadline, http.client.HTTPSConnection):
    pass


class HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, request):
        return self.do_open(HTTPConnection, request)


class HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(HTTPSConnection, request, context=self._context)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_health(url, timeout, context=None):
    """Bound response size and socket operations; do not follow redirects."""
    handlers = [NoRedirect(), HTTPHandler(), HTTPSHandler(context=context)]
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(origin(url) + "/health", method="GET",
                                     headers={"Accept": "application/json", "Cache-Control": "no-store"})
    deadline = time.monotonic() + timeout
    with opener.open(request, timeout=timeout) as response:
        try:
            if response.status != 200:
                raise ValueError("Health status must be 200")
            body = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Health response deadline")
                chunk = response.read1(min(4096, MAX_BODY + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise ValueError("Health response exceeds size bound")
            if time.monotonic() >= deadline:
                raise TimeoutError("Health response deadline")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("Health must be an object")
            return value
        finally:
            timer = getattr(response, "_capture_timer", None)
            if timer is not None:
                timer.cancel(); timer.join()


def number(value, minimum=0, maximum=1e12):
    return (type(value) in (int, float) and math.isfinite(value)
            and minimum <= value <= maximum)


def sanitize(h):
    """Explicit whitelist. Unknown strings become categories, never raw text."""
    fields, invalid = {}, []
    for key in ("camera", "camera_drive_ready", "camera_capture_verified", "replay"):
        value = h.get(key)
        fields[key] = value if type(value) is bool else None
        if type(value) is not bool:
            invalid.append(key)
    control = h.get("control")
    simulated = control.get("simulated") if isinstance(control, dict) else None
    fields["simulated"] = simulated if type(simulated) is bool else None
    if type(simulated) is not bool:
        invalid.append("simulated")
    source = h.get("camera_source")
    fields["source"] = source if isinstance(source, str) and source in SOURCES else "REPLAY" if isinstance(source, str) and source.startswith("REPLAY") else "OTHER" if source is not None else "MISSING"
    for key in ("camera_frame", "map_epoch", "generation"):
        value = h.get(key)
        valid = type(value) is int and 0 <= value <= 2**53 - 1
        fields[key] = value if valid else None
        if not valid and (key == "camera_frame" or value is not None):
            invalid.append(key)
    for key in NUMBERS:
        value = h.get(key)
        fields[key] = value if number(value, maximum=60000) else None
        if fields[key] is None and (key == "camera_age_ms" or value is not None):
            invalid.append(key)
    error = h.get("camera_error")
    fields["camera_error_present"] = error is not None
    if error is not None and not isinstance(error, str):
        invalid.append("camera_error")
    diagnostics = h.get("camera_capture_diagnostics")
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    clock = diagnostics.get("clock")
    fields["clock"] = clock if isinstance(clock, str) and clock in CLOCKS else "OTHER" if clock is not None else "MISSING"
    fields["diagnostics"] = {}
    for key in DIAGNOSTICS:
        value = diagnostics.get(key)
        fields["diagnostics"][key] = value if number(value) else None
        if value is not None and fields["diagnostics"][key] is None:
            invalid.append(key)
    fields["invalid_fields"] = invalid
    return fields


class Evidence:
    def __init__(self):
        self.previous = None
        self.repeat_bound = None
        self.samples = []
        self.advances = self.repeats = self.resets = 0

    def add(self, h, elapsed_s, latency_ms):
        data = sanitize(h)
        issues = ["invalid:" + key for key in data["invalid_fields"]]
        identity = tuple(data[k] for k in ("source", "map_epoch", "generation", "camera_frame"))
        frame = data["camera_frame"]
        event = "initial"
        if self.previous is not None:
            if identity[:3] != self.previous[:3] or (frame is not None and self.previous[3] is not None and frame < self.previous[3]):
                event = "reset"; self.resets += 1; issues.append("identity_reset")
            elif identity == self.previous:
                event = "repeat"; self.repeats += 1
            elif frame is not None and self.previous[3] is not None and frame > self.previous[3]:
                event = "advance"; self.advances += 1
            else:
                event = "invalid_identity"; issues.append(event)
        age = data["camera_age_ms"]
        bound = age + latency_ms if age is not None else None
        if event == "repeat" and bound is not None and self.repeat_bound is not None:
            bound = max(bound, self.repeat_bound[0] + (elapsed_s - self.repeat_bound[1]) * 1000)
        if event != "repeat" and bound is not None:
            self.repeat_bound = (bound, elapsed_s)
        if data["camera"] is not True:
            issues.append("camera_not_fresh")
        if bound is None or bound >= CAMERA_MAX_AGE_MS:
            issues.append("camera_age_unqualified")
        if data["camera_error_present"]:
            issues.append("camera_error")
        viewing = not any(key in issues for key in ("camera_not_fresh", "camera_age_unqualified", "camera_error")) and frame is not None
        if data["simulated"] is not False or data["source"] == "SIMULATED":
            issues.append("simulation_unqualified")
        if data["replay"] is not False or data["source"] == "REPLAY":
            issues.append("replay_unqualified")
        if data["camera_drive_ready"] is not True:
            issues.append("drive_gate_unqualified")
        if data["camera_capture_verified"] is not True:
            issues.append("capture_gate_unqualified")
        if data["source"] != "PHONE RGB":
            issues.append("unsupported_physical_source")
        if data["clock"] != "avcapture_sync_to_host":
            issues.append("unsupported_capture_clock")
        for key in ("sensor_host_delta_ms", "sensor_age_ms"):
            if not number(data["diagnostics"][key], maximum=SENSOR_MAX_AGE_MS):
                issues.append("unqualified:" + key)
        sample = dict(elapsed_s=round(elapsed_s, 6), request_ms=round(latency_ms, 3),
                      health=data, frame_event=event, camera_receipt_bound_ms=bound,
                      viewing_fresh=viewing, physical_capture_eligible=not issues, issues=issues)
        self.samples.append(sample)
        self.previous = identity
        return sample

    def error(self, elapsed_s, latency_ms, category):
        self.samples.append(dict(elapsed_s=round(elapsed_s, 6), request_ms=round(latency_ms, 3),
            error=category, viewing_fresh=False, physical_capture_eligible=False, issues=["network_or_response_error"]))

    def report(self, elapsed_s, interrupted=False):
        issues = sorted({issue for s in self.samples for issue in s["issues"]})
        if self.advances == 0:
            issues.append("no_frame_progress")
        if interrupted:
            issues.append("interrupted")
        def summary(values):
            return dict(median=statistics.median(values), p95=percentile(values, .95), maximum=max(values)) if values else None
        return dict(version=1, scope="GET /health capture evidence only; no control, producer, display or motors",
            elapsed_s=round(elapsed_s, 6), interrupted=interrupted, samples=len(self.samples),
            advancing_frames=self.advances, repeated_frames=self.repeats, identity_resets=self.resets,
            viewing_fresh_samples=sum(s["viewing_fresh"] for s in self.samples),
            eligible_samples=sum(s["physical_capture_eligible"] for s in self.samples),
            sustained_physical_capture_eligible=bool(self.samples) and not issues,
            issues=issues, request_ms=summary([s["request_ms"] for s in self.samples]),
            camera_receipt_bound_ms=summary([s["camera_receipt_bound_ms"] for s in self.samples if s.get("camera_receipt_bound_ms") is not None]),
            evidence=self.samples)


def collect(url, seconds=60, interval=.5, timeout=2, context=None, fetcher=fetch_health):
    url = origin(url)
    for name, value, low, high in (("seconds", seconds, .1, 300), ("interval", interval, .02, 10), ("timeout", timeout, .1, 5)):
        if not number(value, low, high):
            raise ValueError(f"{name} must be finite in {low}..{high}")
    if math.ceil(seconds / interval) > MAX_SAMPLES:
        raise ValueError("Requested sample count exceeds 2000")
    evidence = Evidence()
    started = time.monotonic(); deadline = started + seconds
    interrupted = False
    try:
        while time.monotonic() < deadline:
            sent = time.monotonic()
            try:
                h = fetcher(url, min(timeout, deadline - sent), context)
                received = time.monotonic()
                evidence.add(h, received - started, (received - sent) * 1000)
            except (OSError, ValueError, RecursionError, http.client.HTTPException) as exc:
                now = time.monotonic()
                evidence.error(now - started, (now - sent) * 1000,
                    "timeout" if isinstance(exc, (TimeoutError, socket.timeout)) else "http_error" if isinstance(exc, urllib.error.HTTPError) else "invalid_response" if isinstance(exc, (ValueError, RecursionError)) else "network_error")
                if isinstance(exc, urllib.error.HTTPError):
                    exc.close()
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(interval, remaining))
    except KeyboardInterrupt:
        interrupted = True
    return evidence.report(time.monotonic() - started, interrupted)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8443")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--interval", type=float, default=.5)
    parser.add_argument("--timeout", type=float, default=2)
    parser.add_argument("--ca-file")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    try:
        result = collect(args.url, args.seconds, args.interval, args.timeout, tls_context(args.ca_file))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    encoded = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.out:
        args.out.write_text(encoded)
    else:
        print(encoded, end="")
    print(f"Capture eligibility {'PASS' if result['sustained_physical_capture_eligible'] else 'FAIL'}: "
          f"{result['eligible_samples']}/{result['samples']} samples, {result['advancing_frames']} advances", file=sys.stderr)
    return 0 if result["sustained_physical_capture_eligible"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
