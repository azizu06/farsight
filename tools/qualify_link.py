"""Measure the actual relay path while sending stop-only control probes.

This checks RTT and video throughput, not camera-to-headset latency or motor
watchdogs. Run the physical failure rehearsal in docs/transport-setup.md too.
"""

import argparse
import asyncio
from contextlib import suppress
import json
import math
from pathlib import Path
import ssl
import statistics
import time

import aiohttp

from .operator_relay import upstream_origin


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def capture_receipt_bound(age_ms, elapsed_ms, server_wait_ms=None):
    """Capture receipt-to-arrival upper bound without synchronizing clocks.

    Subtract only the server's measured wait for a fragment, which is already
    represented in capture age when appropriate. Uplink and downlink time stay
    in the bound. Invalid/missing wait metadata keeps the conservative full RTT.
    """
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
           for value in (age_ms, elapsed_ms)):
        raise ValueError("Invalid capture age or request elapsed time")
    wait = server_wait_ms if (type(server_wait_ms) in (int, float)
            and math.isfinite(server_wait_ms) and 0 <= server_wait_ms <= elapsed_ms) else 0
    return age_ms + elapsed_ms - wait


async def measure(origin, seconds=30, video=True, ca_file=None, max_rtt_ms=200, video_mode="auto"):
    start = time.monotonic()
    errors, samples, health = [], [], {}
    stream_bytes = 0
    video_ages = []
    receipt_bounds = []
    selected_video = "none"
    measuring_done = False
    tls = ssl.create_default_context(cafile=ca_file)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=tls),
            timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=2)) as client:
        async def consume_video():
            nonlocal stream_bytes
            try:
                if selected_video == "h264":
                    async with client.ws_connect(str(origin) + "/ws/video",
                            timeout=aiohttp.ClientWSTimeout(ws_close=0.25)) as ws:
                        first = await asyncio.wait_for(ws.receive_json(), 5)
                        if first.get("type") != "video_init":
                            raise ValueError(str(first))
                        stream_bytes += len(await asyncio.wait_for(ws.receive_bytes(), 2))
                        request_id, requested = 0, {}
                        async def request_next():
                            nonlocal request_id
                            request_id += 1
                            requested[request_id] = time.monotonic()
                            await ws.send_json({"type": "next", "request_id": request_id})
                        await request_next()
                        await request_next()
                        while True:
                            metadata = await asyncio.wait_for(ws.receive_json(), 2)
                            identifier = metadata.get("request_id")
                            if (metadata.get("type") != "video_segment"
                                    or type(identifier) is not int or identifier not in requested):
                                raise ValueError("Unexpected video segment metadata")
                            stream_bytes += len(await asyncio.wait_for(ws.receive_bytes(), 2))
                            elapsed_ms = (time.monotonic() - requested.pop(identifier)) * 1000
                            receipt_bounds.append(capture_receipt_bound(
                                metadata.get("age_ms"), elapsed_ms, metadata.get("server_wait_ms")))
                            video_ages.append(metadata["age_ms"])
                            await request_next()
                else:
                    async with client.get(str(origin) + "/stream.mjpg") as response:
                        response.raise_for_status()
                        async for chunk in response.content.iter_chunked(65536):
                            stream_bytes += len(chunk)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
                if not measuring_done:
                    errors.append("video: " + type(exc).__name__)

        stream_task = None
        try:
            async with client.get(str(origin) + "/health") as response:
                response.raise_for_status()
                health = await response.json()
            if video:
                selected_video = ("h264" if health.get("video", {}).get("ready") else "mjpeg") if video_mode == "auto" else video_mode
                stream_task = asyncio.create_task(consume_video())
            async with client.ws_connect(str(origin) + "/ws/control",
                    timeout=aiohttp.ClientWSTimeout(ws_close=0.25)) as ws:
                first = await asyncio.wait_for(ws.receive_json(), 2)
                session_id = first.get("session_id")
                if not session_id:
                    raise ValueError("Control server did not supply a session_id")
                seq = max(0, int(first.get("seq", -1)) + 1)
                until = time.monotonic() + seconds
                while time.monotonic() < until:
                    sent = time.monotonic()
                    # This tool cannot request movement, even when a permit is valid.
                    await ws.send_json({"session_id": session_id, "seq": seq,
                                        "deadman": False, "v": 0, "w": 0})
                    while True:
                        message = await asyncio.wait_for(ws.receive_json(), 1)
                        if message.get("session_id", session_id) != session_id:
                            raise ValueError("Control session changed")
                        if message.get("seq") == seq:
                            samples.append((time.monotonic() - sent) * 1000)
                            break
                    seq += 1
                    await asyncio.sleep(max(0, 0.1 - (time.monotonic() - sent)))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError) as exc:
            errors.append("control/health: " + type(exc).__name__ + ": " + str(exc))
        finally:
            if stream_task:
                measuring_done = True
                stream_task.cancel()
                # Abort its underlying sockets too: cancellation racing a
                # completed wait_for receive must not leave a producer running.
                await client.close()
                with suppress(asyncio.CancelledError):
                    await stream_task
    elapsed = time.monotonic() - start
    p95 = percentile(samples, 0.95)
    capture_p95 = percentile(receipt_bounds, 0.95)
    capture_check = selected_video != "h264" or (capture_p95 is not None and capture_p95 <= 500)
    checks_passed = (not errors and bool(samples) and p95 <= max_rtt_ms
                     and (not video or stream_bytes > 0) and capture_check)
    return {
        "measurement_only": True,
        "motion_requested": False,
        "url": str(origin),
        "seconds": round(elapsed, 2),
        "control_samples": len(samples),
        "control_rtt_median_ms": round(statistics.median(samples), 2) if samples else None,
        "control_rtt_p95_ms": round(p95, 2) if p95 is not None else None,
        "control_rtt_max_ms": round(max(samples), 2) if samples else None,
        "video_bytes": stream_bytes,
        "video_mode": selected_video,
        "observed_video_mbps": round(stream_bytes * 8 / elapsed / 1e6, 3),
        "server_reported_fragment_age_p95_ms": percentile(video_ages, 0.95),
        "capture_receipt_bound_p95_ms": capture_p95,
        "capture_receipt_check_passed": capture_check if selected_video == "h264" else None,
        "h264_request_window": 2 if selected_video == "h264" else None,
        "health": health,
        "errors": errors,
        "transport_checks_passed": checks_passed,
        "not_verified": ["Quest WebXR/USB", "camera-to-display age", "available upload headroom",
                         "physical stop timing", "phone disconnect recovery", "15-minute battery rehearsal"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080", help="Operator relay origin")
    parser.add_argument("--seconds", type=float, default=30)
    parser.add_argument("--no-video", action="store_true", help="Measure only health and stopped control")
    parser.add_argument("--video-mode", choices=("auto", "mjpeg", "h264"), default="auto",
                        help="Auto measures H264 when ready, otherwise MJPEG")
    parser.add_argument("--ca-file")
    parser.add_argument("--max-rtt-ms", type=float, default=200, help="Transport check threshold; not a motor-safety guarantee")
    parser.add_argument("--output", type=Path, help="Save measurements as JSON")
    args = parser.parse_args()
    if not 0 < args.seconds <= 3600 or not 0 < args.max_rtt_ms < 10000:
        parser.error("Choose positive duration <=3600 seconds and RTT threshold <10000 ms")
    try:
        origin = upstream_origin(args.url)
        report = asyncio.run(measure(origin, args.seconds, not args.no_video, args.ca_file, args.max_rtt_ms, args.video_mode))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    text = json.dumps(report, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text + "\n")
    raise SystemExit(0 if report["transport_checks_passed"] else 1)


if __name__ == "__main__":
    main()
