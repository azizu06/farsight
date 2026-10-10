"""Poll the rover /health until every subsystem is up; print one line per subsystem.

python -m tools.ready --url https://farsight.local:8443 --insecure
Run it the moment you power the Jetson: the final line gives total time since boot.
Exit 0 when ready, 1 on timeout. Stdlib only, so it runs on the Jetson or the Mac.
"""

import argparse
import os
import re
import subprocess
import sys
import time

from .netutil import fetch_json, tls_context

ROWS = ("camera", "detector", "rover", "video")


def evaluate(h, skip=(), require_tensorrt=False):
    """Return [(name, ok|wait|fail|skip, detail)] for camera, detector, rover, video (+ assistant when reported).

    `skip` names rows to ignore (bring-up without the Uno or H.264); `require_tensorrt` fails a CPU detector.
    """
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
        backend = h.get("detector_backend") or h.get("detector")
        latency = f", {h['detector_inference_ms']} ms/frame" if h.get("detector_inference_ms") is not None else ""
        detail = f"{backend} {h['detector_fps']:.1f} fps{latency}"
        if require_tensorrt and "tensorrt" not in str(backend):
            rows.append(("detector", "fail", f"{detail}: CPU backend, expected TensorRT (check the .engine path and detector_error)"))
        else:
            rows.append(("detector", "ok", detail))
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
    assistant = h.get("assistant")
    if assistant is not None:
        parts = [label if assistant.get(key) else f"no {label}"
                 for key, label in (("vision_model", "vision"), ("whisper", "whisper"), ("piper", "piper"))]
        rows.append(("assistant", "ok" if any(assistant.get(k) for k in ("vision_model", "whisper", "piper")) else "skip",
                     ", ".join(parts)))
    return [(name, "skip", "skipped by --skip") if name in skip else (name, state, detail) for name, state, detail in rows]


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
    return fetch_json(url, "/health", context, timeout)


def probe(url, timeout=120, interval=0.5, boot_seconds=seconds_since_boot, out=sys.stdout, context=None,
          skip=(), require_tensorrt=False):
    started = time.monotonic()
    last = None
    while True:
        try:
            rows = [("server", "ok", url)] + evaluate(fetch(url, context), skip, require_tensorrt)
        except (OSError, ValueError) as exc:
            rows = [("server", "wait", f"{url} unreachable ({type(exc).__name__})")]
        lines = [f"{name:<10}{state.upper():<5}{detail}" for name, state, detail in rows]
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
    parser.add_argument("--skip", default="", help=f"Comma-separated rows to ignore, from: {', '.join(ROWS)} (camera-only bring-up: rover,video)")
    parser.add_argument("--require-tensorrt", action="store_true", help="Fail while the detector runs on CPU (ONNX/HOG)")
    args = parser.parse_args()
    skip = tuple(name for name in args.skip.replace(",", " ").split() if name)
    unknown = [name for name in skip if name not in ROWS]
    if unknown:
        parser.error(f"--skip: unknown row {', '.join(unknown)} (choose from {', '.join(ROWS)})")
    sys.exit(probe(args.url, timeout=args.timeout, context=tls_context(args.ca_file, args.insecure),
                   skip=skip, require_tensorrt=args.require_tensorrt))


if __name__ == "__main__":
    main()
