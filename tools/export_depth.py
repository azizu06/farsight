"""Export the official metric indoor model on a laptop for offline rover use.

python -m tools.export_depth --local-only
Optional export dependencies are never imported by the rover runtime.
"""

import argparse
import hashlib
import json
from pathlib import Path
import tempfile

MODEL_ID = "depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf"
REVISION = "8078d68a9c75a972131914f6afd0c1723be0da7f"
MODEL_NAME = "depth_anything_v2_metric_indoor_small_308x406.onnx"
INPUT_SHAPE = (1, 3, 308, 406)
OUTPUT_SHAPE = (1, 308, 406)
MEAN = (.485, .456, .406)
STD = (.229, .224, .225)


class ExportError(RuntimeError):
    pass


def validate_contract(session):
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if len(inputs) != 1 or tuple(inputs[0].shape) != INPUT_SHAPE or inputs[0].type != "tensor(float)":
        raise ExportError(f"Expected one FP32 input {INPUT_SHAPE}")
    if len(outputs) != 1 or tuple(outputs[0].shape) != OUTPUT_SHAPE or outputs[0].type != "tensor(float)":
        raise ExportError(f"Expected one FP32 metric-depth output {OUTPUT_SHAPE}")


def validate_prediction(actual, reference):
    import numpy as np
    if actual.shape != OUTPUT_SHAPE or reference.shape != OUTPUT_SHAPE:
        raise ExportError("Prediction has the wrong fixed output shape")
    if not np.isfinite(actual).all() or not np.isfinite(reference).all() or (actual < 0).any():
        raise ExportError("Prediction must contain finite, nonnegative metric depth")
    if not np.allclose(actual, reference, atol=2e-4, rtol=2e-4):
        raise ExportError("ONNX output does not match the original HF PyTorch model")
    return float(np.max(np.abs(actual - reference)))


def export_depth(output_dir, *, local_only=False, overwrite=False):
    try:
        import numpy as np
        import onnx
        import onnxruntime as ort
        import torch
        import transformers
        from huggingface_hub import try_to_load_from_cache
        from transformers import AutoModelForDepthEstimation
    except ImportError as exc:
        raise ExportError("Install laptop dependencies: python -m pip install torch transformers onnx onnxruntime safetensors") from exc

    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / MODEL_NAME
    manifest_path = target.with_suffix(".manifest.json")
    if not overwrite and (target.exists() or manifest_path.exists()):
        raise ExportError(f"Output already exists: {target}; use --overwrite deliberately")
    torch.set_num_threads(2)
    try:
        model = AutoModelForDepthEstimation.from_pretrained(
            MODEL_ID, revision=REVISION, local_files_only=local_only,
            use_safetensors=True, trust_remote_code=False, attn_implementation="eager").eval().cpu()
    except Exception as exc:
        mode = "cached local files" if local_only else "the pinned official HF checkpoint"
        raise ExportError(f"Could not load {mode}: {exc}") from exc
    if model.config.depth_estimation_type != "metric" or model.config.max_depth != 20:
        raise ExportError("Checkpoint is not the expected indoor metric model (max_depth=20 m)")

    class FixedDepth(torch.nn.Module):
        def __init__(self, model):
            super().__init__()
            self.model = model

        def forward(self, pixel_values):
            return self.model(pixel_values=pixel_values).predicted_depth

    # Fixed sizes are deliberate: both dimensions are multiples of the 14-pixel
    # backbone patch size and match jetson.depth, including direct RGB resize.
    wrapper = FixedDepth(model).eval()
    normalized = np.random.default_rng(14).uniform(0, 1, INPUT_SHAPE).astype(np.float32)
    normalized = (normalized - np.array(MEAN, np.float32)[None, :, None, None]) / np.array(STD, np.float32)[None, :, None, None]
    samples = [np.zeros(INPUT_SHAPE, np.float32), np.ascontiguousarray(normalized)]
    with tempfile.TemporaryDirectory(prefix="depth-export-", dir=output_dir) as directory:
        prepared = Path(directory) / MODEL_NAME
        with torch.inference_mode():
            torch.onnx.export(wrapper, torch.from_numpy(samples[0]), str(prepared),
                              input_names=["pixel_values"], output_names=["predicted_depth"],
                              opset_version=17, dynamo=False, do_constant_folding=True)
        onnx.checker.check_model(str(prepared))
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        session = ort.InferenceSession(str(prepared), sess_options=options, providers=["CPUExecutionProvider"])
        validate_contract(session)
        errors = []
        for sample in samples:
            with torch.inference_mode():
                reference = wrapper(torch.from_numpy(sample)).numpy()
            actual = session.run(None, {session.get_inputs()[0].name: sample})[0]
            errors.append(validate_prediction(actual, reference))
        weights = try_to_load_from_cache(MODEL_ID, "model.safetensors", revision=REVISION)
        weights_hash = hashlib.sha256(Path(weights).read_bytes()).hexdigest() if isinstance(weights, str) else None
        manifest = dict(model=MODEL_NAME, sha256=hashlib.sha256(prepared.read_bytes()).hexdigest(),
                        bytes=prepared.stat().st_size, source=f"https://huggingface.co/{MODEL_ID}",
                        revision=REVISION, checkpoint_sha256=weights_hash,
                        input_shape=list(INPUT_SHAPE), output_shape=list(OUTPUT_SHAPE),
                        input_contract="FP32 RGB, directly resized to 406x308, divided by 255 and ImageNet normalized",
                        image_mean=list(MEAN), image_std=list(STD), output_contract="optical-axis metric depth in metres; no per-view normalization",
                        opset=17, dynamic=False, max_depth_m=20, local_only=local_only,
                        exporter=dict(torch=torch.__version__, transformers=transformers.__version__,
                                      onnx=onnx.__version__, onnxruntime=ort.__version__),
                        parity_max_absolute_error_m=errors, parity_atol=2e-4, parity_rtol=2e-4,
                        parity_samples="normalized zero and seeded ImageNet-normalized RGB",
                        hardware_verified=False)
        prepared_manifest = Path(directory) / manifest_path.name
        prepared_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
        # Failed loading/export/parity never exposes a half-written model.
        prepared.replace(target)
        prepared_manifest.replace(manifest_path)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("models"))
    parser.add_argument("--local-only", action="store_true", help="Use the pinned HF cache without network downloads")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(export_depth(args.output_dir, local_only=args.local_only, overwrite=args.overwrite), indent=2))
    except (ExportError, OSError, RuntimeError, ValueError) as exc:
        parser.exit(1, f"Depth export failed: {exc}\n")
    return 0


if __name__ == "__main__":
    main()
