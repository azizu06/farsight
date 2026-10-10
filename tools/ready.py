"""Poll the rover /health until every subsystem is up; print one line per subsystem.

python -m tools.ready --url https://farsight.local:8443 --insecure
Run it the moment you power the Jetson: the final line gives total time since boot.
Exit 0 when ready, 1 on timeout. Stdlib only, so it runs on the Jetson or the Mac.
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.request


def evaluate(h):
    """Return [(name, ok|wait|fail|skip, detail)] for camera, detector, rover, video."""
    rows = []
    if h.get("camera"):
        rows.append(("camera", "ok", f"{h.get('camera_source', 'camera')} {h.get('camera_age_ms', '?')} ms old"))
    else:
        rows.append(("camera", "wait", h.get("camera_error") or "no fresh frame yet"))
    if h.get("detector") == "none":
        rows.append(("detector", "skip", "disabled"))
    elif h.get("detector_error"):
        rows.append(("detector", "fail", h["detector_error"]))
    elif h.get("detector_fps"):
        rows.append(("detector", "ok", f"{h.get('detector')} {h['detector_fps']:.1f} fps"))
    else:
        rows.append(("detector", "wait", f"{h.get('detector')} no detections yet"))
    if h.get("rover"):
        simulated = (h.get("control") or {}).get("simulated")
        rows.append(("rover", "ok", "simulated, no motors" if simulated else "Uno connected"))
    else:
        rows.append(("rover", "wait", "Uno not connected (check USB serial)"))
    video = h.get("video") or {}
    if not video.get("enabled"):
        rows.append(("video", "skip", "H.264 off, MJPEG only"))
    elif video.get("error"):
        rows.append(("video", "fail", video["error"]))
    elif video.get("ready"):
        rows.append(("video", "ok", "H.264 init ready"))
    else:
        rows.append(("video", "wait", "encoder starting"))
    return rows


def is_ready(rows):
    return all(state in ("ok", "skip") for _, state, _ in rows)


def seconds_since_boot():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except OSError:
        pass
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True, timeout=2).stdout
        return time.time() - int(re.search(r"sec = (\d+)", out).group(1))
    except (OSError, AttributeError, subprocess.SubprocessError):
        return None


def fetch(url, context, timeout=2):
    with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=timeout, context=context) as response:
        return json.load(response)


def probe(url, timeout=120, interval=0.5, boot_seconds=seconds_since_boot, out=sys.stdout, context=None):
    started = time.monotonic()
    last = None
    while True:
        try:
            rows = [("server", "ok", url)] + evaluate(fetch(url, context))
        except (OSError, ValueError) as exc:
            rows = [("server", "wait", f"{url} unreachable ({type(exc).__name__})")]
        lines = [f"{name:<9}{state.upper():<5}{detail}" for name, state, detail in rows]
        if lines != last:
            print(f"[+{time.monotonic() - started:5.1f}s]", *lines, sep="\n  ", file=out, flush=True)
            last = lines
        elapsed = time.monotonic() - started
        if is_ready(rows):
            boot = boot_seconds()
            print(f"READY in {elapsed:.1f}s of probing" + (f"; {boot:.0f}s since boot" if boot is not None else ""),
                  file=out, flush=True)
            return 0
        if elapsed >= timeout:
            print(f"NOT READY after {timeout:.0f}s: " + ", ".join(n for n, s, _ in rows if s in ("wait", "fail")),
                  file=out, flush=True)
            return 1
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("FARSIGHT_URL", "http://127.0.0.1:8443"))
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--ca-file", help="CA PEM for private Jetson TLS")
    parser.add_argument("--insecure", action="store_true", help="Skip certificate verification (local self-signed check)")
    args = parser.parse_args()
    context = None
    if args.insecure:
        context = ssl._create_unverified_context()
    elif args.ca_file:
        context = ssl.create_default_context(cafile=args.ca_file)
    sys.exit(probe(args.url, timeout=args.timeout, context=context))


if __name__ == "__main__":
    main()
