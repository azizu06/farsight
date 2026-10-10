"""Record the pitch numbers from a running server, read-only toward the rover.

python -m tools.pitch_metrics --url http://127.0.0.1:8443 --seconds 30 --output pitch.json --markdown pitch.md

Attach to the Jetson directly or through the operator relay. Phases: MJPEG
stream, then H.264 when /health reports it ready. Detector fps (polled /health)
and stopped-control round trips run for the whole attach, so control RTT is
measured under video load. Probes are deadman-released zero commands: nothing
moves and no permit is returned to the server. `--hold-permit` additionally
sends zero-velocity held-deadman heartbeats to read the server's own permit age
(`link_ms`); it never requests motion, but it does take the control lease, so
use it only on a stopped or simulated rover.

Not measured here: glass-to-glass latency and stop distance need a camera and a
tape measure; see docs/pitch-metrics.md. Control has one owner: close the Quest
page first.
"""

import argparse
import asyncio
from contextlib import suppress
import json
from pathlib import Path
import platform
import ssl
import statistics
import time

import aiohttp

from .measure import MjpegParser, summarize_detector, summarize_stream
from .operator_relay import upstream_origin
from .qualify_link import percentile

PENDING = "pending rig run"


def stats(values, digits=1):
    """p50/p95/max of a sample list; all None when empty."""
    if not values:
        return dict(n=0, p50=None, p95=None, max=None)
    return dict(n=len(values), p50=round(statistics.median(values), digits),
                p95=round(percentile(values, .95), digits), max=round(max(values), digits))


def summarize_segments(segments, seconds):
    """H.264 delivery: `segments` is [(arrival_s, frames, bytes, age_ms)] in arrival order.

    fps counts frames over the arrival span (first segment's frames excluded,
    as measure.summarize_stream does for MJPEG); age is the server-reported
    capture age, not camera-to-display latency.
    """
    span = segments[-1][0] - segments[0][0] if len(segments) > 1 else 0
    frames = sum(frames for _, frames, _, _ in segments[1:])
    ages = [age for *_, age in segments if isinstance(age, (int, float))]
    return dict(segments=len(segments), seconds=round(seconds, 2),
                fps=round(frames / span, 1) if span > 0 else 0,
                mbit_per_s=round(sum(size for _, _, size, _ in segments) * 8 / 1e6 / seconds, 2) if seconds > 0 else 0,
                server_age_ms=stats(ages))


def summarize_control(rtts, permit_ages, permit_gaps, link_ms, errors=()):
    return dict(rtt_ms=stats(rtts, 2), permit_age_at_send_ms=stats(permit_ages),
                permit_interval_ms=stats(permit_gaps), server_link_ms=stats(link_ms),
                errors=list(errors))


async def poll_health(client, origin, stop, samples, interval=0.5):
    while not stop.is_set():
        with suppress(aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            async with client.get(origin + "/health") as response:
                samples.append(await response.json())
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), interval)


async def control_probes(client, origin, stop, out, hold_permit=False, period=0.1):
    """Stop-only probes; `out` collects rtts, permit ages/gaps, link_ms, errors."""
    latest = {"permit": None, "at": None}
    waiters = {}

    async def reader(ws):
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            status = json.loads(message.data)
            now = time.monotonic()
            if status.get("permit"):
                if latest["at"] is not None:
                    out["permit_gaps"].append((now - latest["at"]) * 1000)
                latest.update(permit=status["permit"], at=now)
            waiter = waiters.get(status.get("seq"))
            if waiter and not waiter.done():
                waiter.set_result((now, status))

    try:
        async with client.ws_connect(origin + "/ws/control",
                                     timeout=aiohttp.ClientWSTimeout(ws_close=0.25)) as ws:
            first = await asyncio.wait_for(ws.receive_json(), 2)
            session_id = first.get("session_id")
            if not session_id:
                raise ValueError("Control server did not supply a session_id")
            seq = max(0, int(first.get("seq", -1)) + 1)
            read_task = asyncio.create_task(reader(ws))
            try:
                while not stop.is_set():
                    sent = time.monotonic()
                    if latest["at"] is not None:
                        out["permit_ages"].append((sent - latest["at"]) * 1000)
                    command = {"session_id": session_id, "seq": seq, "deadman": False, "v": 0, "w": 0}
                    if hold_permit and latest["permit"]:
                        command.update(deadman=True, permit=latest["permit"], mode="teleop",
                                       video_fresh=True, video_age_ms=0, pan=0)
                    waiters[seq] = asyncio.get_running_loop().create_future()
                    await ws.send_json(command)
                    try:
                        arrived, status = await asyncio.wait_for(waiters[seq], 1)
                    except asyncio.TimeoutError:
                        out["errors"].append("control: reply timeout")
                    else:
                        out["rtts"].append((arrived - sent) * 1000)
                        if hold_permit and isinstance(status.get("link_ms"), (int, float)):
                            out["link_ms"].append(status["link_ms"])
                    waiters.pop(seq, None)
                    seq += 1
                    await asyncio.sleep(max(0, period - (time.monotonic() - sent)))
            finally:
                read_task.cancel()
                with suppress(asyncio.CancelledError):
                    await read_task
                if hold_permit:  # explicit release so the lease never outlives the run
                    with suppress(aiohttp.ClientError, ConnectionError):
                        await ws.send_json({"session_id": session_id, "seq": seq, "deadman": False, "v": 0, "w": 0})
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        out["errors"].append("control: " + type(exc).__name__ + ": " + str(exc))


async def measure_mjpeg(client, origin, seconds):
    parser, started = MjpegParser(), time.monotonic()
    try:
        async with client.get(origin + "/stream.mjpg") as response:
            response.raise_for_status()
            async for chunk in response.content.iter_chunked(65536):
                parser.feed(chunk)
                if time.monotonic() - started >= seconds:
                    break
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return dict(error=type(exc).__name__ + ": " + str(exc))
    parser.finish()
    return summarize_stream(parser.frames, time.monotonic() - started)


async def measure_h264(client, origin, seconds):
    segments, started = [], time.monotonic()
    try:
        async with client.ws_connect(origin + "/ws/video",
                                     timeout=aiohttp.ClientWSTimeout(ws_close=0.25)) as ws:
            first = await asyncio.wait_for(ws.receive_json(), 5)
            if first.get("type") != "video_init":
                raise ValueError(str(first))
            await asyncio.wait_for(ws.receive_bytes(), 2)
            request_id = 0

            async def request_next():
                nonlocal request_id
                request_id += 1
                await ws.send_json({"type": "next", "request_id": request_id})

            await request_next()
            await request_next()
            while time.monotonic() - started < seconds:
                meta = await asyncio.wait_for(ws.receive_json(), 2)
                if meta.get("type") != "video_segment":
                    raise ValueError("Unexpected video segment metadata")
                data = await asyncio.wait_for(ws.receive_bytes(), 2)
                segments.append((time.monotonic(), max(1, len(meta.get("samples") or [])),
                                 len(data), meta.get("age_ms")))
                await request_next()
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
        return dict(error=type(exc).__name__ + ": " + str(exc))
    return summarize_segments(segments, time.monotonic() - started)


def source_label(health):
    """'simulated' when the camera or rover is explicitly synthetic; otherwise 'rig'."""
    simulated = (str(health.get("camera_source", "")).upper().startswith("SIMULATED")
                 or (health.get("control") or {}).get("simulated") is True)
    return "simulated" if simulated else "rig"


async def collect(origin, seconds=30, ca_file=None, hold_permit=False, video=True):
    tls = ssl.create_default_context(cafile=ca_file)
    health_samples, control = [], dict(rtts=[], permit_ages=[], permit_gaps=[], link_ms=[], errors=[])
    phases = {}
    stop = asyncio.Event()
    started = time.monotonic()
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls),
            timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=3)) as client:
        async with client.get(origin + "/health") as response:
            response.raise_for_status()
            health = await response.json()
        health_samples.append(health)
        tasks = [asyncio.create_task(poll_health(client, origin, stop, health_samples)),
                 asyncio.create_task(control_probes(client, origin, stop, control, hold_permit))]
        try:
            if video:
                phases["mjpeg"] = await measure_mjpeg(client, origin, seconds)
                if (health.get("video") or {}).get("ready"):
                    phases["h264"] = await measure_h264(client, origin, seconds)
                else:
                    phases["h264"] = dict(skipped="/health video.ready is false (start the server with --h264)")
            else:
                await asyncio.sleep(seconds)
        finally:
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)
    label = source_label(health)
    return dict(
        schema=1, url=origin, label=label, seconds_per_phase=seconds,
        elapsed_s=round(time.monotonic() - started, 1), host=platform.platform(),
        measured_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        read_only=not hold_permit, motion_requested=False, hold_permit=hold_permit,
        camera_source=health.get("camera_source"), rover_simulated=(health.get("control") or {}).get("simulated"),
        detector=summarize_detector(health_samples),
        stream=phases, control=summarize_control(
            control["rtts"], control["permit_ages"], control["permit_gaps"], control["link_ms"], control["errors"]),
        load="control probes ran while MJPEG then H.264 were streaming" if video else "control probes with no video",
        manual_pending=dict(glass_to_glass=PENDING, stop_distance=PENDING),
    )


def cell(value, unit=""):
    return PENDING if value is None else f"{value}{unit}"


def render_markdown(report):
    """README-ready table. Rig-only metrics always read 'pending rig run' here."""
    where = "simulated (laptop)" if report["label"] == "simulated" else "rig"
    det, stream, ctl = report["detector"], report["stream"], report["control"]
    mjpeg, h264 = stream.get("mjpeg", {}), stream.get("h264", {})
    rtt, age = ctl["rtt_ms"], ctl["permit_age_at_send_ms"]
    detector_how = (f"{det.get('backend')} backend, median of `/health.detector_fps` polled every 0.5 s "
                    f"({det.get('samples')} samples)")
    rows = [
        ("Detector fps", cell(det.get("fps_median"), " fps") if det.get("samples") else PENDING,
         detector_how, where),
        ("Stream fps, MJPEG", cell(mjpeg.get("fps"), " fps") if "fps" in mjpeg else PENDING,
         "frames parsed from `/stream.mjpg` over %s s" % report["seconds_per_phase"], where),
        ("Stream fps, H.264", cell(h264.get("fps"), " fps") if "fps" in h264 else PENDING,
         "frames in `/ws/video` segments over %s s" % report["seconds_per_phase"], where),
        ("Control round-trip, p50 / p95",
         f"{rtt['p50']} / {rtt['p95']} ms" if rtt["n"] else PENDING,
         f"stopped `/ws/control` probe to its echoed status, {rtt['n']} probes at 10 Hz under video load", where),
        ("Permit age at send, p50 / p95",
         f"{age['p50']} / {age['p95']} ms" if age["n"] else PENDING,
         "age of the newest received permit at each probe; limit is 250 ms", where),
        ("Glass-to-glass latency", PENDING, "stopwatch filmed through the system, median of 10; docs/pitch-metrics.md", "rig"),
        ("Stop distance after deadman release", PENDING, "tape-marked floor, 5 trials; docs/pitch-metrics.md", "rig"),
    ]
    lines = ["| Metric | Value | How measured | Source |", "|---|---|---|---|"]
    lines += [f"| {m} | {v} | {h} | {s} |" for m, v, h, s in rows]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="Jetson or operator relay origin")
    parser.add_argument("--seconds", type=float, default=30, help="Seconds per video phase")
    parser.add_argument("--no-video", action="store_true", help="Skip stream phases (detector and control only)")
    parser.add_argument("--hold-permit", action="store_true",
                        help="Also send zero-velocity held-deadman heartbeats to read server link_ms")
    parser.add_argument("--ca-file")
    parser.add_argument("--output", type=Path, help="Write JSON report")
    parser.add_argument("--markdown", type=Path, help="Write the markdown table")
    args = parser.parse_args(argv)
    if not 0 < args.seconds <= 600:
        parser.error("Choose a phase duration in (0, 600] seconds")
    try:
        origin = str(upstream_origin(args.url))
        report = asyncio.run(collect(origin, args.seconds, args.ca_file, args.hold_permit, not args.no_video))
    except (ValueError, OSError, aiohttp.ClientError) as exc:
        parser.exit(2, f"pitch_metrics: {exc}\n")
    table = render_markdown(report)
    print(table)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    if args.markdown:
        args.markdown.write_text(table)
    errors = report["control"]["errors"] + [p["error"] for p in report["stream"].values() if "error" in p]
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
