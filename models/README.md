Not in git (see .gitignore). Get them from a teammate's laptop or re-download:

| File | Source |
|---|---|
| yolo11n.onnx + .labels.json + .manifest.json | `python3 -m tools.export_detector` on a laptop; detection export for the live rover detector |
| yolo11n-pose.onnx | `yolo export model=yolo11n-pose.pt format=onnx imgsz=640 opset=17 simplify=True` |
| depth_anything_v2_metric_indoor_small_308x406.onnx | HF `depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf`, exported with torch.onnx (input 1x3x308x406, output metres) |
| Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf + mmproj Q8_0 | HF `ggml-org/Qwen2.5-VL-3B-Instruct-GGUF` |
| SmolVLM-500M-Instruct-Q8_0.gguf + mmproj Q8_0 | HF `ggml-org/SmolVLM-500M-Instruct-GGUF` (fast fallback) |
| ggml-base.en.bin | HF `ggerganov/whisper.cpp` |

The live detector uses ordinary **detection** outputs `[1,84,8400]`, not the pose
model listed above. Prepare `yolo11n.onnx` before going offline, copy `models/`
with `tools/push_models.sh user@JETSON`, and build the TensorRT engine on the
Jetson. The exporter also writes human-readable class labels and an SHA-256
manifest. See `docs/inference-setup.md` for the GPU path when available.

CPU rehearsal: `--detector-model models/yolo11n.onnx`. With no model, the server
uses the bundled HOG CPU person detector. HOG is a fallback and does not establish
that TensorRT or GPU inference is working. Ultralytics export dependencies are
needed on the laptop only; the Jetson inference path does not require PyTorch.

## Optional rescue-drill face detector (Hugging Face Hub)

- Hub repo: [`onnxmodelzoo/version-RFB-320`](https://huggingface.co/onnxmodelzoo/version-RFB-320).
- File: `version-RFB-320.onnx`, revision `6fd293d22b523ec88959f104b8eef5395e3adfbc` (1.27 MB).
- License: the model card's License section and SPDX header say **MIT**;
  Hub metadata says **Apache-2.0**. Both are permissive; preserve this provenance
  discrepancy rather than claiming a single unambiguous label.
- SHA-256: `34cd7e60aeff28744c657de7a3dc64e872d506741de66987f3426f2b79f88017`.

Download from the Hub into a **new** file (the exclusive create refuses overwrite):

```bash
python - <<'PY'
from urllib.request import urlopen
url = 'https://huggingface.co/onnxmodelzoo/version-RFB-320/resolve/6fd293d22b523ec88959f104b8eef5395e3adfbc/version-RFB-320.onnx'
with open('models/version-RFB-320.onnx', 'xb') as target:
    target.write(urlopen(url).read())
PY
python -m jetson.server --face-model models/version-RFB-320.onnx
```

`--face-model PATH` enables an extra pass alongside HOG, YOLO detection or pose;
with `--detector none` it runs face-only. Omit the flag to keep it off (default).
The model path is configurable; no runtime download or shared-model-folder writes.
Normalized `cls: "face"` boxes join the existing detection packet and receive
stable, class-separated track IDs. No new packet fields or interface changes.

UltraFace directly resizes RGB to 320x240, normalizes `(pixel - 127) / 128`,
and outputs `[1,N,2]` scores and `[1,N,4]` normalized xyxy boxes. It reuses pose
NMS, but deliberately does not use YOLO letterboxing/decoding. Default face
confidence is 0.7 and NMS IoU is 0.3. CPU uses onnxruntime when installed, otherwise
OpenCV DNN. The ONNX is suitable for a later TensorRT build; this face module
currently runs ONNX CPU only and does not load `.engine` files.

Mac rehearsal (arm64, macOS 27.0.1, OpenCV 4.14.0 CPU): 10 warmup + 100 seeded
random 640x480 frames, face preprocessing/inference/decode included: mean **3.09 ms**,
median **3.07 ms**, p95 **3.40 ms** added per frame. This is synthetic local input,
not Jetson throughput or face accuracy evidence. Validate faces and build/measure
TensorRT on the Jetson before claiming GPU or rescue-drill performance.

```bash
FARSIGHT_FACE_MODEL=models/version-RFB-320.onnx python -m pytest jetson/tests/test_face.py -q
```

The real-model smoke test skips if the configured file is absent.
