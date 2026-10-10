"""Local-only Python bulk-pacing qualification; never opens physical devices.

python -m tools.qualify_python_bulk --report docs/qualification/python-bulk-pacing.json
"""

import argparse
import asyncio
from collections import defaultdict
import gzip
import hashlib
import json
import math
from pathlib import Path
import platform
import struct
import time

import aiohttp

from tools.qualify_link import percentile
from tools.tests.test_demo_qualification import Controller, Rig, VideoClient, fixture_app, wait_until


def bulk_fixture(args):
    import numpy as np
    from fastapi import Request

    app = fixture_app(args.serve_fixture, server_options=dict(
        detector="pose", detector_model=str(args.model), bulk_limit_mbps=args.bulk_limit))
    # A fixed valid point packet is a sizing fixture, not a claimed measured map.
    rng = np.random.default_rng(20261010)
    packet = np.zeros(200000, dtype=[("xyz", "<f4", (3,)), ("rgb", "u1", (3,)), ("pad", "u1")])
    packet["xyz"] = rng.uniform(-5, 5, (200000, 3)).astype("<f4")
    packet["rgb"] = rng.integers(0, 256, (200000, 3), np.uint8)
    body = struct.pack("<I", len(packet)) + packet.tobytes()
    app.state.mapping.point_packet = lambda: body
    counts = defaultdict(lambda: dict(bytes=0, messages=0, first_at=None, last_at=None))

    class SourceMeter:
        def __init__(self, app):
            self.app = app
        async def __call__(self, scope, receive, send):
            path = scope.get("path", "")
            async def measured_send(message):
                await send(message)
                if message["type"] not in ("http.response.body", "websocket.send") or path.startswith("/__"):
                    return
                data = message.get("body") or message.get("bytes") or message.get("text") or b""
                size = len(data.encode() if isinstance(data, str) else data)
                key = path + (":binary" if message.get("bytes") is not None else ":body"
                              if message["type"] == "http.response.body" else ":text")
                row = counts[key]
                row["bytes"] += size
                row["messages"] += 1
                now = time.monotonic()
                row["first_at"] = now if row["first_at"] is None else row["first_at"]
                row["last_at"] = now
            await self.app(scope, receive, measured_send)
    app.add_middleware(SourceMeter)

    @app.api_route("/__bulk_qualification", methods=["GET", "DELETE"])
    async def metrics(request: Request):
        if request.method == "DELETE":
            counts.clear()
        return dict(source=dict(counts), pacer_enabled=app.state.bulk.enabled,
            pacer_pending=app.state.bulk.pending_requests,
            future_debt_ms=max(0, (app.state.bulk._finish or 0) - time.monotonic()) * 1000,
            model=app.state.perception.health(), snapshot_raw_bytes=len(body),
            snapshot_gzip_bytes=len(gzip.compress(body)), snapshot_sha256=hashlib.sha256(body).hexdigest())
    app.router.routes.sort(key=lambda route: 0 if getattr(route, "path", "").startswith("/__") else 1)
    return app


async def source_metrics(rig, clear=False):
    async with rig.client.request("DELETE" if clear else "GET", rig.direct + "/__bulk_qualification") as response:
        response.raise_for_status()
        return await response.json()


async def trial(rate, scenario, model, duration=12):
    async with Rig("rgb15", bandwidth_mbps=2, delay_ms=50, fixture_module="tools.qualify_python_bulk",
                   fixture_args=["--bulk-limit", str(rate), "--model", str(model)]) as rig:
        video = VideoClient(rig.url)
        controller = Controller(rig, video)
        map_bytes, map_headers, map_error, map_done = 0, {}, None, False
        status_messages, binary_messages, status_ack = 0, 0, False
        snapshot = subscription = None
        phases, samples = [], []
        client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=5, sock_read=5),
                                       auto_decompress=False)

        async def download():
            nonlocal map_bytes, map_headers, map_error, map_done
            try:
                async with client.get(rig.url + "/map/points.bin", headers={"Accept-Encoding": "gzip"}) as response:
                    response.raise_for_status()
                    map_headers = response.headers.copy()
                    async for block in response.content.iter_chunked(16384):
                        map_bytes += len(block)
                map_done = True
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                map_error = type(exc).__name__

        async def subscribe():
            nonlocal status_messages, binary_messages, status_ack
            async with client.ws_connect(rig.url + "/ws/map?points=http") as ws:
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        status_messages += 1
                        status_ack |= message.json().get("points_transport") == "http"
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        binary_messages += 1

        def phase(name, at, source):
            bulk_bytes = sum(row["bytes"] for key, row in source["source"].items()
                             if key == "/map/points.bin:body" or key == "/ws/video:binary")
            phases.append(dict(name=name, at=at, source=source, source_bulk_bytes=bulk_bytes,
                               delivered_map_bytes=map_bytes, received_fragments=len(video.bounds),
                               video_connected=video.connected, video_age_ms=video.current_age()
                               if math.isfinite(video.current_age()) else None))

        try:
            await video.start()
            await controller.start()
            await wait_until(lambda: len(video.bounds) >= 10 and video.current_age() <= 400, timeout=8)
            await controller.release()
            await wait_until(lambda: not controller.status.get("requires_release") and video.current_age() <= 400)
            if not controller.press():
                raise RuntimeError("Explicit simulated rover and fresh view required")
            initial = await source_metrics(rig, clear=True)
            started = time.monotonic()
            baseline_fragments = len(video.bounds)
            proxy_before = rig.proxy.stats["bytes"]
            subscription = asyncio.create_task(subscribe())
            snapshot = asyncio.create_task(download())
            aborted = False
            phase("start", 0, initial)
            while time.monotonic() - started < duration:
                elapsed = time.monotonic() - started
                if scenario == "abort" and not aborted and elapsed >= 3:
                    snapshot.cancel()
                    await client.close()
                    await asyncio.gather(snapshot, subscription, return_exceptions=True)
                    aborted = True
                    phase("after_abort", elapsed, await source_metrics(rig))
                status = await rig.status()
                samples.append(dict(at=elapsed, moving=bool(status["left_pwm"] or status["right_pwm"]),
                                    video_age_ms=video.current_age() if math.isfinite(video.current_age()) else None))
                await asyncio.sleep(.05)
            elapsed = time.monotonic() - started
            final = await source_metrics(rig)
            phase("finish", elapsed, final)
            backend = await rig.fixture_status()
            settled = [sample for sample in samples if sample["at"] >= 1]
            recovery = [sample for sample in samples if sample["at"] >= 5]
            return dict(bulk_limit_mbps=rate, scenario=scenario, seconds=elapsed,
                modeled_link=dict(mbps=2, delay_each_direction_ms=50), guards=dict(permit_ms=250, view_ms=500),
                explicitly_simulated_motors=True, actual_pose_workload=final["model"],
                snapshot_raw_bytes=final["snapshot_raw_bytes"], snapshot_gzip_bytes=final["snapshot_gzip_bytes"],
                snapshot_delivered_bytes=map_bytes, snapshot_complete=map_done,
                snapshot_content_encoding=map_headers.get("Content-Encoding"), snapshot_error=map_error,
                snapshot_intentionally_aborted=aborted, status_only_ack=status_ack,
                status_messages=status_messages, map_binary_messages=binary_messages,
                source_bulk_bytes=phases[-1]["source_bulk_bytes"],
                source_bulk_offered_mbps=phases[-1]["source_bulk_bytes"] * 8 / elapsed / 1e6,
                proxy_total_mbps=(rig.proxy.stats["bytes"] - proxy_before) * 8 / elapsed / 1e6,
                control_ack_p95_ms=percentile(controller.acks, .95),
                accepted_permit_age_p95_ms=percentile([event["permit_age_ms"] for event in backend["command_events"]
                    if event["accepted"] and event["permit_age_ms"] is not None], .95),
                first_backend_failure=backend["first_failure"], capture_receipt_p95_ms=percentile(video.bounds, .95),
                received_video_fragments=len(video.bounds) - baseline_fragments,
                video_errors=video.errors, control_errors=controller.errors,
                client_invalidations=controller.invalidations.copy(),
                settled_moving_fraction=sum(sample["moving"] for sample in settled) / max(1, len(settled)),
                recovery_moving_fraction=sum(sample["moving"] for sample in recovery) / max(1, len(recovery)),
                recovery_view_peak_ms=max((sample["video_age_ms"] for sample in recovery
                    if sample["video_age_ms"] is not None), default=None),
                final_pacer_pending=final["pacer_pending"], final_future_debt_ms=final["future_debt_ms"], phases=phases)
        finally:
            for task in (snapshot, subscription):
                if task:
                    task.cancel()
            await client.close()
            await asyncio.gather(*(task for task in (snapshot, subscription) if task), return_exceptions=True)
            await controller.close()
            await video.close()


async def run(args):
    result = dict(scope="Software simulation and modeled local link; not Jetson/cellular/Quest timing",
        host=platform.platform(), pose_model=str(args.model), production_modified=False, trials=[])
    for scenario in ("snapshot", "abort"):
        for rate in (0, 1.4):
            measured = await trial(rate, scenario, args.model, args.duration)
            result["trials"].append(measured)
            print(f"Recorded {scenario}, bulk={rate}: ack p95 {measured['control_ack_p95_ms']}, "
                  f"capture p95 {measured['capture_receipt_p95_ms']}", flush=True)
            if args.report:
                args.report.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-fixture", choices=("rgb15",))
    parser.add_argument("--port", type=int)
    parser.add_argument("--bulk-limit", type=float, default=0)
    parser.add_argument("--model", type=Path, default=Path("models/yolo11n-pose.onnx"))
    parser.add_argument("--duration", type=float, default=12)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.serve_fixture:
        import uvicorn
        uvicorn.run(bulk_fixture(args), host="127.0.0.1", port=args.port, access_log=False, log_level="warning",
                    timeout_graceful_shutdown=2)
    else:
        if not args.model.is_file() or not 6 <= args.duration <= 60:
            parser.error("A local pose model and duration in 6..60 seconds are required")
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(run(args))
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
