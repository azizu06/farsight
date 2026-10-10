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
