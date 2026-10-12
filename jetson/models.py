"""On-rover model inventory for GET /health: which local models run, their source repo, and last latency."""
import re
from pathlib import Path

# Basename pattern -> (name, source, repo). Unknown files keep their basename and no repo claim.
CATALOG = (
    (r"yolo11n-pose", "YOLO11n-pose", "ultralytics", "Ultralytics YOLO11 (AGPL-3.0, not Hugging Face)"),
    (r"qwen3-?vl-2b", "Qwen3-VL-2B", "huggingface", "Qwen/Qwen3-VL-2B-Instruct-GGUF"),
    (r"qwen2\.5-vl-3b", "Qwen2.5-VL-3B", "huggingface", "ggml-org/Qwen2.5-VL-3B-Instruct-GGUF"),
    (r"smolvlm", "SmolVLM-500M", "huggingface", "ggml-org/SmolVLM-500M-Instruct-GGUF"),
    (r"ggml-(\w+\.en)", "Whisper {}", "huggingface", "ggerganov/whisper.cpp"),
    (r"^([a-z]{2}_[A-Z]{2}-[\w-]+)\.onnx$", "Piper {}", "huggingface", "rhasspy/piper-voices"),
    (r"version-RFB-320", "UltraFace RFB-320", "huggingface", "onnxmodelzoo/version-RFB-320"),
    (r"depth_anything_v2_metric_indoor_small", "Depth Anything V2 Metric Indoor Small", "huggingface",
     "depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf"),
)
UNIT_DIR = Path("/etc/systemd/system")


def identify(path):
    """(name, source, repo) for a model file path; None when no path is configured."""
    if not path:
        return None
    base = Path(str(path)).name
    for pattern, name, source, repo in CATALOG:
        match = re.search(pattern, base, re.IGNORECASE)
        if match:
            return name.format(*match.groups()), source, repo
    return base, "local", None


def unit_model(unit, variable, unit_dir=None):
    """The model file a separate llama/whisper service was installed with (its unit's Environment=)."""
    try:
        text = (Path(unit_dir or UNIT_DIR) / unit).read_text()
    except OSError:
        return None
    match = re.search(rf'^Environment="?{variable}=([^"\n]+)"?\s*$', text, re.MULTILINE)
    return match.group(1).strip() if match else None


def entry(role, path, loaded, last_ms):
    known = identify(path)
    if known is None:
        return None
    name, source, repo = known
    return dict(role=role, name=name, source=source, repo=repo, file=Path(str(path)).name,
                loaded=bool(loaded), last_ms=last_ms)


def inventory(detector=None, vision=None, whisper=None, voices=(), extra=()):
    """List of model entries. Each argument is (path, loaded, last_ms); voices and extra are (role, path, loaded, last_ms)."""
    rows = [entry("detector", *detector) if detector else None,
            entry("vision", *vision) if vision else None,
            entry("stt", *whisper) if whisper else None]
    rows += [entry(*voice) for voice in voices] + [entry(*item) for item in extra]
    return [row for row in rows if row is not None]
