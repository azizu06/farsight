# Farsight

A rover's-eye reconnaissance workspace: **iPhone Pro LiDAR → Jetson Orin Nano → browser / Quest 2**.
Synchronized RGB, measured depth, confidence, and ARKit camera poses build a colored 3D map as the rover moves.

The current implementation includes a Python mapping server, bounded voxel fusion, observed surface reconstruction,
live RGB/depth feeds, a Three.js 3D and top-down viewer, WebXR map inspection, and PLY export.
It accepts the existing [Pip Scout phone stream](https://github.com/azizu06/pip-scout/blob/main/docs/INTERFACES.md).
The new ingestion, fusion service, surface builder, simulator, and web view were implemented in this repo.

## Run the hardware-free demo

Python 3.10+ and Node 20.19+ or 22.12+:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r jetson/mapping/requirements.txt
npm --prefix quest ci
npm --prefix quest run build
.venv/bin/python -m jetson.mapping.server --demo
```

Open **http://localhost:8443**. The clearly labeled simulator ray-casts a room and sends its RGB-D observations
through the real mapper. Right-drag to orbit, left-drag to pan, scroll to zoom, **F** to frame the scan,
and **P** to switch points/hybrid. **Escape** opens feeds, view modes, pause, export, and VR entry.

## Connect the phone

Run the same server **without `--demo`** on the Jetson. Join the phone and viewer to the Jetson's LAN,
and configure the Pip Scout iPhone app's backend URL as `ws://JETSON_IP:8443/phone`.
Open `http://JETSON_IP:8443` on your laptop. Quest immersive VR needs trusted HTTPS; see [mapping setup](docs/mapping.md).

For frontend development, keep the Python server on 8443 and run `npm --prefix quest run dev`.
Vite serves port 5173 and proxies the mapping endpoints to Python.

## Verify

```bash
.venv/bin/python -m pip install -r jetson/mapping/requirements-test.txt
.venv/bin/python -m unittest discover -s jetson/mapping/tests -v
npm --prefix quest test
npm --prefix quest run build
```

See [mapping architecture and wire format](docs/mapping.md). The earlier [rover/control contract](docs/interfaces.md)
remains separate: this mapper does not command motors or implement detections.

## Model licenses

Pose detection uses Ultralytics YOLO11n-pose (`models/yolo11n-pose.onnx`, not committed), licensed AGPL-3.0. Run it with
`python -m jetson.mapping.server --detector pose --detector-model models/yolo11n-pose.onnx` (`pip install onnxruntime` is
optional; OpenCV DNN is the fallback). Detection boxes then carry `kpts` and `down`. The TensorRT pose backend is a stub until the engine is built on the Jetson.
