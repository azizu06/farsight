# UltraFace CPU budget on the Jetson

The optional UltraFace RFB-320 pass now uses two ONNX Runtime intra-operation
threads. The CPU provider, output decoding, confidence threshold, OpenCV fallback,
and all runtime guards stay unchanged.

Two repeated, interleaved comparisons ran on the actual six-core Jetson using
`models/version-RFB-320.onnx` and the existing public-photo inference fixture.
Each block created an isolated session, warmed it five times, then timed the
production `FaceDetector.detect()` path, including preprocessing and NMS.

| ORT thread budget | First pass median / p95 | Confirmation median / p95 |
|---|---:|---:|
| Default | 43.89 / 55.32 ms | 30.66 / 54.42 ms |
| One | 25.49 / 26.07 ms | 26.05 / 26.55 ms |
| Two | 17.28 / 17.76 ms | 17.51 / 18.05 ms |

Each setting had 80 measured calls in the first pass and 75 in confirmation.
All returned the same two faces, with zero normalized box/confidence difference
on this image. The default session added five worker threads and used roughly
four CPU cores during the measured block; two threads added one worker and used
roughly two cores. This reduced CPU contention while improving the loaded
component's latency. ONNX Runtime documents the thread-budget behavior in its
[thread management guide](https://onnxruntime.ai/docs/performance/tune-performance/threading.html).

Native CUDA builds remained running at approximately one CPU core each, alongside
`llama-server`. These are loaded-board component results, not clean hardware
latency, detector-stream FPS, dataset accuracy, or camera-to-eye measurements.
Session startup and JPEG disk decoding are excluded. Profiling changed no target
source, services, sockets, package installations, or motor configuration.

Raw comparison summaries, per-block confirmation numbers, hashes, versions and
preserved load are in [jetson-face-threads.json](qualification/jetson-face-threads.json).
Root will deploy the source change and verify the integrated runtime separately.
