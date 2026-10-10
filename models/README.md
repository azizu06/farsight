Not in git (see .gitignore). Get them from a teammate's laptop or re-download:

| File | Source |
|---|---|
| yolo11n-pose.onnx | `yolo export model=yolo11n-pose.pt format=onnx imgsz=640 opset=17 simplify=True` |
| depth_anything_v2_metric_indoor_small_308x406.onnx | HF `depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf`, exported with torch.onnx (input 1x3x308x406, output metres) |
| Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf + mmproj Q8_0 | HF `ggml-org/Qwen2.5-VL-3B-Instruct-GGUF` |
| SmolVLM-500M-Instruct-Q8_0.gguf + mmproj Q8_0 | HF `ggml-org/SmolVLM-500M-Instruct-GGUF` (fast fallback) |
| ggml-base.en.bin | HF `ggerganov/whisper.cpp` |
