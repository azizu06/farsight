"""Desk measurement of the Mac visual stream. Not a headset measurement.

python -m tools.mac_render.bench --upstream http://127.0.0.1:8443
python -m tools.mac_render.bench --synthetic --fps 30 --bitrate 4000000

--synthetic skips the server and browser: it pipes generated JPEG frames
through the real screencast_to_ffmpeg command and Mp4Reader, so encoder
flags can be measured anywhere ffmpeg runs (including the Mac with
VideoToolbox).
"""

import argparse
import asyncio
import json
import resource
import shutil
import subprocess
import threading
import time

import websockets

from .encode import screencast_to_ffmpeg
from .mp4 import Mp4Reader, fragment_timing
from .protocol import frame_age_ms, percentile, pose_to_frame_ms
from .stats import summarize


async def measure(origin, seconds, poses):
    view = origin.replace("http", "ws", 1) + "/ws/macview"
    pose_url = origin.replace("http", "ws", 1) + "/ws/macview-pose"
    samples = []
    started = time.monotonic()
    async with websockets.connect(view, max_size=8_000_000) as socket:
        pose_socket = await websockets.connect(pose_url) if poses else None
        init = json.loads(await socket.recv())
        if init.get("macview") != "macview_init" and init.get("type") != "macview_init":
            raise RuntimeError(f"expected macview_init, got {init}")
        await socket.recv()  # fMP4 init bytes
        request_id = 0
        while time.monotonic() - started < seconds:
            request_id += 1
            if pose_socket is not None:
                await pose_socket.send(json.dumps({
                    "type": "pose", "version": 1,
                    "sent_mono_ms": time.monotonic() * 1000,
                    "sent_wall_ms": time.time() * 1000,
                    "head": {"position": [0, 1.6, 3], "quaternion": [0, 0, 0, 1]},
                    "ray": {"origin": [0, 1.6, 3], "direction": [0, 0, -1]},
                }))
            requested = time.monotonic()
            await socket.send(json.dumps({"type": "next", "request_id": request_id}))
            sidecar = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            payload = await asyncio.wait_for(socket.recv(), timeout=5)
            now = time.time() * 1000
            served_ms = (time.monotonic() - requested) * 1000
            samples.append({
                "bytes": len(payload),
                "served_ms": served_ms,
                "first_fragment_ms": (time.monotonic() - started) * 1000 if request_id == 1 else None,
                "render_ms": sidecar.get("render_ms"),
                "capture_ms": sidecar.get("capture_ms"),
                "encode_ms": sidecar.get("encode_ms"),
                "frame_age_ms": frame_age_ms(sidecar["capture_wall_ms"], now),
                "pose_to_frame_ms": pose_to_frame_ms(sidecar.get("pose"), sidecar["capture_mono_ms"]),
            })
        if pose_socket is not None:
            await pose_socket.close()
    return samples, init


def generate_jpegs(width, height, fps, count):
    """Moving synthetic content as a JPEG-per-frame list, split on EOI."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg unavailable")
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate={fps}",
         "-frames:v", str(count), "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
        capture_output=True, timeout=120, check=True)
    frames = []
    data = result.stdout
    cursor = 0
    while True:
        end = data.find(b"\xff\xd9", cursor)
        if end < 0:
            break
        frames.append(data[cursor:end + 2])
        cursor = end + 2
    if not frames:
        raise RuntimeError("synthetic capture produced no JPEG frames")
    return frames


def measure_synthetic(width, height, fps, bitrate, seconds, hardware=None):
    """Pipe generated JPEGs through the real encode args + Mp4Reader."""
    args, encoder = screencast_to_ffmpeg(width, height, fps, bitrate, hardware)
    frames = generate_jpegs(width, height, fps, int(seconds * fps) + fps)
    reader = Mp4Reader()
    process = subprocess.Popen(args, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    started = time.monotonic()
    deadline = started + seconds
    events = {"init_ms": None, "first_fragment_ms": None}
    fragment_sizes, fragment_counts, fragment_gaps = [], [], []
    fed = [0]
    last_fragment_at = [None]
    encoded = [0]

    def feed():
        interval = 1 / fps
        for frame in frames:
            if time.monotonic() > deadline + 1:
                return
            try:
                process.stdin.write(frame)
                process.stdin.flush()
            except (BrokenPipeError, ValueError):
                return
            fed[0] += 1
            wait = started + fed[0] * interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)

    def read_out():
        last_fragment_at_local = None
        while True:
            chunk = process.stdout.read1(65536)
            if not chunk:
                return
            for kind, data in reader.feed(chunk):
                now = (time.monotonic() - started) * 1000
                if kind == "init":
                    events["init_ms"] = now
                    continue
                if events["first_fragment_ms"] is None:
                    events["first_fragment_ms"] = now
                if last_fragment_at_local is not None:
                    fragment_gaps.append(now - last_fragment_at_local)
                last_fragment_at_local = now
                last_fragment_at[0] = now
                fragment_sizes.append(len(data))
                first, count, _start, _end = fragment_timing(data, fps)
                fragment_counts.append(count)
                encoded[0] += count

    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    writer = threading.Thread(target=feed, daemon=True)
    output = threading.Thread(target=read_out, daemon=True)
    writer.start()
    output.start()
    writer.join(timeout=max(2, deadline + 2 - time.monotonic()))
    try:
        process.stdin.close()
    except (BrokenPipeError, ValueError):
        pass
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    output.join(timeout=3)
    wall = (last_fragment_at[0] / 1000) if last_fragment_at[0] else time.monotonic() - started
    fed, encoded = fed[0], encoded[0]
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_ms = (usage.ru_utime - before.ru_utime + usage.ru_stime - before.ru_stime) * 1000
    return {
        "label": "Synthetic ffmpeg pipe. Encode path only; no render, no network.",
        "encoder": encoder,
        "resolution": f"{width}x{height}",
        "fps_requested": fps,
        "bitrate": bitrate,
        "seconds": round(wall, 3),
        "frames_fed": fed,
        "frames_encoded": encoded,
        "fragments": len(fragment_sizes),
        "init_ms": events["init_ms"],
        "first_fragment_ms": events["first_fragment_ms"],
        "fragment_interval_ms_p50": percentile(fragment_gaps, 0.50),
        "fragment_interval_ms_p95": percentile(fragment_gaps, 0.95),
        "samples_per_fragment_p50": percentile(fragment_counts, 0.50),
        "fragment_bytes_p50": percentile(fragment_sizes, 0.50),
        "fragment_bytes_max": max(fragment_sizes, default=None),
        "encode_cpu_ms_per_frame": round(cpu_ms / encoded, 3) if encoded else None,
        "encode_cpu_percent": round(cpu_ms / (wall * 1000) * 100, 1) if wall else None,
        "fps_sustained": round(encoded / wall, 2) if wall else None,
        "bytes_per_s": round(sum(fragment_sizes) / wall, 1) if wall else None,
        "argv": " ".join(args),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default="http://127.0.0.1:8090")
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--no-pose", action="store_true")
    parser.add_argument("--out", help="Write the JSON report here")
    parser.add_argument("--synthetic", action="store_true",
                        help="Measure the ffmpeg pipe directly; no server or browser")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--bitrate", type=int, default=4_000_000)
    parser.add_argument("--hardware", choices=("auto", "videotoolbox", "libx264"), default="auto")
    args = parser.parse_args(argv)
    if args.synthetic:
        report = measure_synthetic(
            args.width, args.height, args.fps, args.bitrate, args.seconds,
            None if args.hardware == "auto" else args.hardware == "videotoolbox")
        text = json.dumps(report, indent=2)
        if args.out:
            with open(args.out, "w") as handle:
                handle.write(text + "\n")
        print(text)
        return report
    samples, init = asyncio.run(measure(args.upstream, args.seconds, not args.no_pose))
    report = summarize(
        samples, args.seconds, init["width"], init["height"], init["fps"],
        init.get("encoder", "see /health"), "Mac desk, no headset",
    )
    report["stereo"] = False
    report["note"] = "Flat pose-driven view. Not stereo. Not a Quest measurement."
    report["fragments_per_s"] = round(len(samples) / args.seconds, 2) if args.seconds else None
    report["first_fragment_ms"] = next(
        (s["first_fragment_ms"] for s in samples if s.get("first_fragment_ms") is not None), None)
    report["served_ms_p50"] = percentile([s["served_ms"] for s in samples], 0.50)
    report["served_ms_p95"] = percentile([s["served_ms"] for s in samples], 0.95)
    report["encode_ms_p95"] = percentile(
        [s["encode_ms"] for s in samples if s.get("encode_ms") is not None], 0.95)
    text = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(text + "\n")
    print(text)
    return report


if __name__ == "__main__":
    main()
