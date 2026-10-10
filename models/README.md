Not in git (see .gitignore). Get them from a teammate's laptop or re-download:

| File | Source |
|---|---|
| yolo11n.onnx + .labels.json + .manifest.json | `python3 -m tools.export_detector` on a laptop; detection export for the live rover detector |
| yolo11n-pose.onnx | `yolo export model=yolo11n-pose.pt format=onnx imgsz=640 opset=17 simplify=True` |
| depth_anything_v2_metric_indoor_small_308x406.onnx + .manifest.json | `python3 -m tools.export_depth` on a laptop; pinned official HF indoor metric model |
| Qwen3VL-2B-Instruct-Q4_K_M.gguf + mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf | HF `Qwen/Qwen3-VL-2B-Instruct-GGUF`; **default assistant model**, `python3 -m tools.fetch_models --groups vlm-qwen3` |
| Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf + mmproj Q8_0 | HF `ggml-org/Qwen2.5-VL-3B-Instruct-GGUF` (older fallback, `--groups vlm`) |
| SmolVLM-500M-Instruct-Q8_0.gguf + mmproj Q8_0 | HF `ggml-org/SmolVLM-500M-Instruct-GGUF` (fast fallback) |
| ggml-base.en.bin | HF `ggerganov/whisper.cpp`; default speech model (`ggml-tiny.en.bin` is the fallback) |

## Assistant vision-language models

Never mix a model with another model's projector; `jetson/envgen.py` only accepts these pairs
automatically and prefers them in this order: Qwen3-VL-2B, SmolVLM-500M, Qwen2.5-VL-3B.
Setup, the Jetson CUDA build and the env lines are in
[`docs/assistant-setup.md`](../docs/assistant-setup.md).

| File | Bytes | SHA-256 | Source |
|---|---|---|---|
| `Qwen3VL-2B-Instruct-Q4_K_M.gguf` | 1,107,409,952 | `089d75c52f4b7ffc56ba998ffc50aae89fcafc755f9e7208aacca281dca6c2ae` | https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF/resolve/main/Qwen3VL-2B-Instruct-Q4_K_M.gguf |
| `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf` | 445,053,216 | `f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82` | https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf |
| `SmolVLM-500M-Instruct-Q8_0.gguf` | 436,806,912 | `9d4612de6a42214499e301494a3ecc2be0abdd9de44e663bda63f1152fad1bf4` | https://huggingface.co/ggml-org/SmolVLM-500M-Instruct-GGUF |
| `mmproj-SmolVLM-500M-Instruct-Q8_0.gguf` | 108,783,360 | `d1eb8b6b23979205fdf63703ed10f788131a3f812c7b1f72e0119d5d81295150` | same repo |
| `Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf` | 1,929,901,056 | `d02fe9b69ad8cadbbd228e387667af66612c44bed29ffc8eb1e7caf9ac486c12` | https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF |
| `mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf` | 844,757,728 | `980c9b2f78c04e6cff93d277ada09e768394f112d75db3b4e9dea8a69f9fb904` | same repo |
| `ggml-base.en.bin` | 147,964,211 | `a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002` | https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin |

The official Qwen repo names its files `Qwen3VL-...` (no hyphen) and ships Q4_K_M; the
`ggml-org` Qwen3-VL-2B repo only has Q8_0 (1.83 GB). Hashes were computed on the Mac after
download (Qwen3 and Qwen2.5 model hashes equal the Hub LFS object ids).

Mac measurements (Apple M5 Pro, Metal, llama.cpp build 11146; **not Jetson numbers**), warm
`SceneAssistant.ask()` vision stage with `--image-max-tokens 256`: Qwen3-VL-2B 548 ms median,
peak RSS 2.7 GB; Qwen2.5-VL-3B 537 ms, 3.0 GB; SmolVLM-500M (no cap, not applicable) 231 ms,
0.97 GB. Full table and answer-quality notes: `docs/assistant-setup.md`.

The live detector uses ordinary **detection** outputs `[1,84,8400]`, not the pose
model listed above. Prepare `yolo11n.onnx` before going offline, copy `models/`
with `tools/push_models.sh user@JETSON`, and build the TensorRT engine on the
Jetson. The exporter also writes human-readable class labels and an SHA-256
manifest. See `docs/inference-setup.md` for the GPU path when available.

CPU rehearsal: `--detector-model models/yolo11n.onnx`. With no model, the server
uses the bundled HOG CPU person detector. HOG is a fallback and does not establish
that TensorRT or GPU inference is working. Ultralytics export dependencies are
needed on the laptop only; the Jetson inference path does not require PyTorch.

## Optional metric-depth export for stationary scans

Prepare this before the demo network becomes unavailable:

```bash
python3 -m pip install torch transformers onnx onnxruntime safetensors
python3 -m tools.export_depth --output-dir models
# Subsequent exports can use only already cached official weights:
python3 -m tools.export_depth --local-only --output-dir /tmp/depth-export
python -m tools.benchmark_scan --model models/depth_anything_v2_metric_indoor_small_308x406.onnx
```

The exporter uses official
[Depth Anything V2 Metric Indoor Small](https://huggingface.co/depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf)
at immutable revision `8078d68a9c75a972131914f6afd0c1723be0da7f`.
The fixed FP32 contract is RGB `[1,3,308,406]`, divided by 255 and normalized
with ImageNet mean/std, to optical-axis metres `[1,308,406]`. The existing
runtime directly resizes to that grid; it does not use the Hub processor's
default aspect-preserving 518-pixel resize. Output is never rescaled per view.
Export includes ONNX checking, exact shape/type validation and numerical
parity against the original PyTorch model on two deterministic inputs. It
refuses existing output unless `--overwrite` is explicitly passed and emits
SHA-256, checkpoint provenance, library versions and parity errors in its manifest.

This laptop's tested exporter versions are Torch 2.14.0, Transformers 5.6.1,
ONNX 1.21.0 and ONNX Runtime 1.24.4. Byte hashes may differ with exporter
versions; the historical artifact hash in `docs/scan.md` is not a download
integrity pin for a new export. Build the FP16 TensorRT engine on the target
Jetson using `python -m jetson.build_engine models/depth_anything_v2_metric_indoor_small_308x406.onnx`.
Neither successful CPU parity nor an engine build measures real-room depth
accuracy or permits using estimated depth as safe driving clearance. See
[`docs/scan.md`](../docs/scan.md) for runtime and hardware gates.

The October 10 Mac export produced SHA-256
`7ba4679f646ac5af720a565727cc8352ccb58a52dfc65fceb5874f8ed880190e`
(99,112,383 bytes). Two deterministic parity samples differed by at most
0.00000501 m; a separate public bus-photo sample differed by 0.0000739 m.
The production HTTP scan benchmark completed five synthetic room views in
2547.53 ms and delivered 89,968 points (1,439,492 bytes), with ONNX Runtime
1.31.0 CPU on this Mac. This is a local software compatibility measurement,
including its current host load, rather than target Jetson performance.

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
