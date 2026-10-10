# Local YOLO inference on the Jetson

`jetson/tensorrt_detector.py` runs a raw TensorRT 10+ engine using the JetPack CUDA
runtime through `ctypes`. It does not require PyTorch, PyCUDA, or CUDA-enabled
OpenCV. It accepts the application's already letterboxed, normalized RGB NCHW
blob and returns raw predictions for `yolo_boxes()` to decode and suppress.

**Hardware status:** CPU tests verify shape policy, checked CUDA bindings, inference
ordering, and resource cleanup. On the physical Orin Nano, a target-built FP16 pose engine
(`[1,56,8400]`) ran 20 zero-input smoke runs at median 15.29 ms / p95 18.83 ms, and COCO
(`[1,84,8400]`) at median 11.66 ms / p95 21.44 ms, under concurrent build load
([jetson-runtime-results.md](jetson-runtime-results.md)). That is engine execution only: it
does not prove camera-to-eye latency, accuracy, or sustained fps under the full pipeline.
Complete the on-device camera checks before enabling the demo detector.

## Model contract

- Input: `[1, 3, 640, 640]`, RGB, floating point, values in `[0,1]` after letterbox
  padding. The caller handles JPEG decoding and letterboxing.
- Output: `[1, 84, 8400]`, four box coordinates followed by 80 COCO class
  probabilities, without embedded NMS or an additional objectness channel.
- Both I/O tensors must be linear, device-resident FP32 or FP16. Only one input
  and one output are supported. Dynamic inputs must resolve to the same 640x640
  shape within optimization profile zero.
- Default contract is YOLOv8/YOLO11 COCO detection. An **explicit** pose contract
  accepts `[1,56,8400]`: four box coordinates, one person probability, then
  17 `(x,y,confidence)` keypoints. `TensorRTPoseDetector` selects that contract
  and reuses the same pose letterbox/decoder as the ONNX backend. Pixel coordinates
  are finite-checked, not mistaken for class probabilities. Segmentation, custom
  class counts, transposed outputs, YOLOv5 objectness, and end-to-end NMS outputs
  remain rejected explicitly.

Startup performs two real warmup inferences, checks finite outputs and class
probability bounds, and fails if execution or model validation fails. It does
not silently substitute CPU detections or fake GPU results.

## Inspect the existing JetPack installation first

On the Jetson, from the repository root:

```sh
cat /etc/nv_tegra_release
python3 -c 'import tensorrt as trt; print(trt.__version__)'
ldconfig -p | rg 'libcudart|libnvinfer'
/usr/local/cuda/bin/nvcc --version
/usr/src/tensorrt/bin/trtexec --help
```

Paths can differ between JetPack installations. Locate `trtexec` using
`command -v trtexec` if the shown path does not exist. Use JetPack's supported
CUDA/TensorRT installation; do not replace it with a random desktop CUDA wheel.
The loader accepts CUDA runtime sonames 13 and 12 and the standard JetPack CUDA
library paths. Every CUDA operation reports its actual error code and message.
[NVIDIA's Orin Nano setup guide](https://docs.nvidia.com/jetson/orin-nano-devkit/user-guide/setup_cuda.html)
describes the platform's CUDA installation.

If using a virtual environment, create it from the Jetson system Python with
`--system-site-packages` so its installed TensorRT Python bindings remain visible:

```sh
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
python -m pip install -r jetson/mapping/requirements.txt -r jetson/control/requirements.txt
python -c 'import tensorrt as trt; print(trt.__version__)'
```

## Build the engine on the target Jetson

The prepared model is `models/yolo11n.onnx`. Copy the model along with the source
before going offline. A serialized engine depends on its build environment and
device; build it on the Jetson that will run the demo. This runner expects a raw
TensorRT plan, such as one produced by `trtexec`.

`python -m jetson.build_engine models/*.onnx` (also run by `jetson/setup.sh`)
wraps the command below, caches by ONNX sha256 and TensorRT version in a
`.engine.json` stamp so reruns are instant, and takes `--shapes images:1x3x640x640`
for dynamic models. The manual equivalent for a static 640x640 ONNX model on TensorRT 10:

```sh
/usr/src/tensorrt/bin/trtexec \
  --onnx=models/yolo11n.onnx \
  --saveEngine=models/yolo11n.engine \
  --fp16 \
  --memPoolSize=workspace:512 \
  --skipInference
```

The 512 MiB workspace is a starting build limit, not the model's complete runtime
memory consumption. Keep adequate free RAM for camera processing and the video
encoder. `--fp16` permits internal FP16 computation; the runner reads the actual
I/O dtype instead of assuming the inputs also become FP16.

If the ONNX model uses dynamic dimensions, add an explicit profile using the
actual ONNX input name (the prepared model uses `images`):

```sh
  --minShapes=images:1x3x640x640 \
  --optShapes=images:1x3x640x640 \
  --maxShapes=images:1x3x640x640
```

These are additional flags on the preceding command, not a separate shell
command. TensorRT 10 uses `--memPoolSize` and `--skipInference` in place of older
`--workspace` and `--buildOnly` flags.
[NVIDIA TensorRT 10 CLI migration guide](https://docs.nvidia.com/deeplearning/tensorrt/10.x.x/api/tensorrt-8x-to-10x-trtexec.html)
and [trtexec reference](https://github.com/NVIDIA/TensorRT/blob/main/samples/trtexec/README.md)
document these flags. If the installed TensorRT is 11 or later, inspect its
`trtexec --help` before building: newer strongly typed export workflows differ
from TensorRT 10's `--fp16` workflow.

## YOLO11-pose on TensorRT (issue #9): Jetson checklist

Untested on hardware: CPU tests fake `tensorrt` and CUDA. On the Jetson, from the
repo root, with `models/yolo11n-pose.onnx` pushed:

```sh
# 1. FP16 engine, 1x3x640x640 in, 1x56x8400 out. Reruns are instant (hash cache).
jetson/.venv/bin/python -m jetson.build_engine models/yolo11n-pose.onnx

# 2. Engine smoke test: shapes, I/O dtype, median/p95 GPU latency on zero input.
jetson/.venv/bin/python -m jetson.tensorrt_detector models/yolo11n-pose.engine --pose --runs 50

# 3. Hardware test (skipped everywhere else).
FARSIGHT_POSE_ENGINE=models/yolo11n-pose.engine jetson/.venv/bin/python -m pytest -q jetson/tests/test_pose_tensorrt.py

# 4. Server on the camera with the pose engine, motors simulated, plain HTTP on 8000.
#    (Service instead: FARSIGHT_DETECTOR=pose + FARSIGHT_DETECTOR_MODEL in /etc/farsight.env.)
jetson/.venv/bin/python -m jetson.server --camera /dev/video0 --simulate-rover \
  --detector pose --detector-model models/yolo11n-pose.engine --port 8000

# 5. With someone in view: detector fps and per-frame latency (decode, letterbox, GPU, NMS).
curl -s localhost:8000/health | python3 -c 'import json,sys; h=json.load(sys.stdin); print({k: v for k, v in h.items() if k.startswith("detector")})'
```

Pass: `detector_backend` is `pose-tensorrt`, `detector_error` is `null`,
`detector_fps` near 15 (the pose loop's TensorRT cap), and
`detector_inference_ms` well under 66 ms. A `detector_error` names the failure
(missing bindings, wrong output shape, engine built by another TensorRT). The
`.onnx` model still runs on CPU as the fallback. Then compare boxes, keypoints,
and `down` in the HUD with the ONNX model on the same scene.

## Smoke test, then validate real camera detections

```sh
python -m jetson.tensorrt_detector models/yolo11n.engine --runs 50
# For a separately built raw yolo11n-pose engine:
python -m jetson.tensorrt_detector models/yolo11n-pose.engine --pose --runs 50
```

This runs actual GPU inference on zero input and prints shape, dtype, median,
and p95 host-observed inference time. It includes memory transfers and output
validation. Passing means the engine executes with the expected interface; it
does not measure detector accuracy or the complete camera pipeline.

The application integration calls:

```python
from jetson.tensorrt_detector import TensorRTDetector

with TensorRTDetector("models/yolo11n.engine") as detector:
    prediction = detector.predict(rgb_nchw_blob)
```

`rgb_nchw_blob` comes from the app's existing JPEG decoding/letterbox path. The
runner serializes inference on one execution context, binds the CUDA device for
each worker thread, and returns a detached FP32 array. `close()` synchronizes and
releases the stream, device allocations, and pinned host allocations. Call it
when the perception service shuts down.

The implementation follows TensorRT's named tensor address setup and
`execute_async_v3`, and resolves dynamic dimensions using the execution context.
[NVIDIA TensorRT Python inference guide](https://docs.nvidia.com/deeplearning/tensorrt/10.x.x/inference-library/python-api-docs.html),
[dynamic shape guide](https://docs.nvidia.com/deeplearning/tensorrt/latest/inference-library/dynamic-shapes-basics.html),
and [CUDA memory API](https://docs.nvidia.com/cuda/cuda-runtime-api/cuda_runtime_api/group__CUDART__MEMORY.html)
describe those APIs.

Before judging, compare ONNX and engine detections on identical real frames;
check a nearby person, a distant person, several people, and an empty scene.
Record inference throughput and CPU/RAM usage with video encoding active. A
detector error must stay visible in `/health` and the HUD. Local obstacle checks
and motor leases remain independent of detector results.

The prepared ONNX file can be regenerated on a separate export computer using
the YOLO11 checkpoint and explicit 640x640, batch-one raw detection export.
[Ultralytics export documentation](https://docs.ultralytics.com/modes/export)
describes the model export arguments. Model export dependencies are separate
from this Jetson runtime; keep downloads and export work outside the offline demo
startup path.

CPU tests:

```sh
python -m unittest jetson.tests.test_tensorrt_detector -v
```
