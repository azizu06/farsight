"""Real local ONNX depth on stationary ray-cast views, through POST /scan and MapState.

python -m tools.benchmark_scan --model /path/to/depth_anything_v2_metric_indoor_small_308x406.onnx
No physical motor port is opened. Includes a fresh operator heartbeat during inference.
"""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import time
from fastapi.testclient import TestClient
import onnxruntime

from jetson.mapping.server import create_app


def benchmark(model):
    app = create_app(demo=True, simulate_rover=True, detector="none", depth_model=str(model))
    with TestClient(app) as client, client.websocket_connect("/ws/control") as ws:
        status = ws.receive_json()
        seq = 0

        def send(held, mode):
            nonlocal seq
            seq += 1
            ws.send_json(dict(session_id=status["session_id"], permit=status["permit"], seq=seq,
                deadman=held, v=0, w=0, pan=0, mode=mode, video_fresh=True, video_age_ms=0))

        send(False, "teleop")
        status = ws.receive_json()
        send(True, "teleop")
        time.sleep(.03)
        response = client.post("/scan", json=dict(session_id=status["session_id"], permit=status["permit"]))
        response.raise_for_status()
        while True:
            status = ws.receive_json()
            result = client.get("/scan").json()
            if not result["running"]:
                send(False, "teleop")
                break
            send(True, "scan")
        if result["state"] != "complete":
            raise RuntimeError(result)
        packet = client.get("/map/points.bin").content
        result.update(packet_bytes=len(packet), model_sha256=hashlib.sha256(model.read_bytes()).hexdigest(),
                      platform=platform.platform(), onnxruntime=onnxruntime.__version__)
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(benchmark(args.model), indent=2))
