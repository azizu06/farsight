"""Desk measurement of the Mac visual stream. Not a headset measurement.

python -m tools.mac_render.bench --upstream http://127.0.0.1:8443
"""

import argparse
import asyncio
import json
import time

import websockets

from .protocol import frame_age_ms, pose_to_frame_ms
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
            await socket.send(json.dumps({"type": "next", "request_id": request_id}))
            sidecar = json.loads(await asyncio.wait_for(socket.recv(), timeout=5))
            payload = await asyncio.wait_for(socket.recv(), timeout=5)
            now = time.time() * 1000
            samples.append({
                "bytes": len(payload),
                "render_ms": sidecar.get("render_ms"),
                "capture_ms": sidecar.get("capture_ms"),
                "encode_ms": sidecar.get("encode_ms"),
                "frame_age_ms": frame_age_ms(sidecar["capture_wall_ms"], now),
                "pose_to_frame_ms": pose_to_frame_ms(sidecar.get("pose"), sidecar["capture_mono_ms"]),
            })
        if pose_socket is not None:
            await pose_socket.close()
    return samples, init


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", default="http://127.0.0.1:8090")
    parser.add_argument("--seconds", type=float, default=8)
    parser.add_argument("--no-pose", action="store_true")
    parser.add_argument("--out", help="Write the JSON report here")
    args = parser.parse_args(argv)
    samples, init = asyncio.run(measure(args.upstream, args.seconds, not args.no_pose))
    report = summarize(
        samples, args.seconds, init["width"], init["height"], init["fps"],
        init.get("encoder", "see /health"), "Mac desk, no headset",
    )
    report["stereo"] = False
    report["note"] = "Flat pose-driven view. Not stereo. Not a Quest measurement."
    text = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(text + "\n")
    print(text)
    return report


if __name__ == "__main__":
    main()
