"""Render /etc/farsight.env from jetson/farsight.env.example for the files that exist on this Jetson.

jetson/install_service.sh runs this once (stdlib only, so it works before the venv exists):
    python3 -m jetson.envgen --repo ~/farsight [--tls-dir /etc/farsight/tls] [--assistant] [--whisper]
It prefers the TensorRT pose engine, then the ONNX pose model, then the bundled HOG detector, and only turns on
optional features (face model, Piper voices, HTTPS, llama/whisper URLs) when their files or units exist, so the
first boot never points at something missing.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

PLACEHOLDER_REPO = "/home/farsight/farsight"
ASSISTANT_URL = "http://127.0.0.1:8092"
WHISPER_URL = "http://127.0.0.1:8091"
SMOLVLM_PAIR = ("SmolVLM-500M-Instruct-Q8_0.gguf", "mmproj-SmolVLM-500M-Instruct-Q8_0.gguf")
QWEN25_PAIR = ("Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf", "mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf")
QWEN3_PAIR = ("Qwen3VL-2B-Instruct-Q4_K_M.gguf", "mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf")
# Every known model is bound to its own projector; automatic choice takes the first complete pair here.
VISION_PAIRS = (SMOLVLM_PAIR, QWEN25_PAIR, QWEN3_PAIR)
AUTO_VISION_ORDER = (QWEN3_PAIR, SMOLVLM_PAIR, QWEN25_PAIR)
WHISPER_ORDER = ("ggml-base.en.bin", "ggml-tiny.en.bin", "ggml-medium.en.bin", "ggml-medium.bin")


def model_selection(repo, exists=os.path.exists, overrides=None):
    """Prefer prepared small models; never cross-pair automatic VLM projectors."""
    overrides = overrides or {}
    models = Path(repo) / "models"
    def configured(*keys):
        return next((str(overrides[key]).strip() for key in keys if overrides.get(key)), "")
    def safe(path):
        if "\n" in path or "\r" in path or "\0" in path:
            raise ValueError("Configured model paths must not contain control characters")
        return path
    def first(names):
        return next((str(models / name) for name in names if exists(str(models / name))), "")
    warnings = []
    model = safe(configured("FARSIGHT_LLM_MODEL"))
    projector = safe(configured("FARSIGHT_LLM_MMPROJ", "FARSIGHT_LLM_PROJECTOR"))
    explicit = bool(model or projector)
    if explicit:
        if model and not projector:
            projector = next((str(Path(model).parent / paired) for name, paired in VISION_PAIRS if Path(model).name == name), "")
        elif projector and not model:
            model = next((str(Path(projector).parent / name) for name, paired in VISION_PAIRS if Path(projector).name == paired), "")
    else:
        for name, paired in AUTO_VISION_ORDER:
            if exists(str(models / name)) and exists(str(models / paired)):
                model, projector = str(models / name), str(models / paired)
                break
    model_family = next((name for name, _ in VISION_PAIRS if Path(model).name == name), None)
    projector_family = next((name for name, paired in VISION_PAIRS if Path(projector).name == paired), None)
    # Recognized cross-family pairs are rejected. Explicit renamed/custom pairs
    # remain caller configuration and must be validated on the target hardware.
    matching = not (model_family and projector_family) or model_family == projector_family
    llm_ready = bool(model and projector and matching and exists(model) and exists(projector))
    if explicit and not llm_ready:
        warnings.append("Configured vision pair is missing, incomplete, or mixes known model families; no automatic replacement was selected")
    whisper = safe(configured("FARSIGHT_WHISPER_SERVER_MODEL", "FARSIGHT_WHISPER_MODEL")) or first(WHISPER_ORDER)
    piper = safe(configured("FARSIGHT_PIPER_MODEL")) or first((
        "en_US-lessac-low.onnx", "piper/en_US-lessac-medium.onnx", "en_US-lessac-medium.onnx"))
    spanish = safe(configured("FARSIGHT_PIPER_MODEL_ES")) or first(("piper/es_MX-claude-high.onnx", "es_MX-claude-high.onnx"))
    return dict(FARSIGHT_LLM_MODEL=model, FARSIGHT_LLM_MMPROJ=projector,
                FARSIGHT_WHISPER_SERVER_MODEL=whisper, FARSIGHT_WHISPER_MODEL=whisper,
                FARSIGHT_PIPER_MODEL=piper, FARSIGHT_PIPER_MODEL_ES=spanish,
                llm_ready=llm_ready, whisper_ready=bool(whisper and exists(whisper)), warnings=warnings)


def env_value(value):
    """EnvironmentFile supports quoted spaces; no shell evaluation is performed."""
    value = str(value)
    if "\n" in value or "\r" in value or "\0" in value:
        raise ValueError("Environment values must be single-line")
    return json.dumps(value) if any(char.isspace() or char in "\\\"'" for char in value) else value


def render_unit(text, repo, src, user, selection, env_file="/etc/farsight.env"):
    replacements = {"USER": user, "REPO": str(repo), "SRC": str(src), "ENV_FILE": str(env_file),
                    "LLM_MODEL": selection["FARSIGHT_LLM_MODEL"], "LLM_MMPROJ": selection["FARSIGHT_LLM_MMPROJ"],
                    "WHISPER_MODEL": selection["FARSIGHT_WHISPER_SERVER_MODEL"]}
    # Quote original executable tokens before inserting a path containing spaces.
    text = re.sub(r"^(ExecStart(?:Pre)?=)(@(?:SRC|REPO)@/\S+)(.*)$", r'\1"\2"\3', text, flags=re.MULTILINE)
    lines = []
    for line in text.splitlines():
        # These directives accept one literal path, not a shell/argv list.
        # systemd keeps interior spaces; adding quotes makes them path characters.
        literal_path = line.startswith(("WorkingDirectory=", "ConditionPathExists=", "EnvironmentFile="))
        for key, value in replacements.items():
            value = str(value)
            if any(char in value for char in "\n\r\0"):
                raise ValueError("Unit template paths must be single-line")
            escaped = value.replace("%", "%%")
            if not literal_path:
                escaped = escaped.replace("\\", "\\\\").replace('"', '\\"')
            line = line.replace(f"@{key}@", escaped)
        if line == "EnvironmentFile=/etc/farsight.env":
            line = "EnvironmentFile=" + str(env_file).replace("%", "%%")
        lines.append(line)
    return "\n".join(lines) + "\n"


def render(example_text, repo, exists=os.path.exists, tls_dir="/etc/farsight/tls", assistant=False, whisper=False,
           camera_source="usb", serial_port=None, overrides=None):
    if camera_source not in ("phone", "usb"):
        raise ValueError("Camera source must be phone or usb")
    models = f"{repo}/models"
    values = {}
    for name in ("yolo11n-pose.engine", "yolo11n-pose.onnx"):
        if exists(f"{models}/{name}"):
            values.update(FARSIGHT_DETECTOR="pose", FARSIGHT_DETECTOR_MODEL=f"{models}/{name}")
            break
    else:
        values.update(FARSIGHT_DETECTOR="hog", FARSIGHT_DETECTOR_MODEL="")
    depth = "depth_anything_v2_metric_indoor_small_308x406"
    values["FARSIGHT_DEPTH_MODEL"] = next((f"{models}/{depth}.{ext}" for ext in ("engine", "onnx")
                                           if exists(f"{models}/{depth}.{ext}")), "")
    for key, relative in (("FARSIGHT_FACE_MODEL", "version-RFB-320.onnx"),):
        values[key] = f"{models}/{relative}" if exists(f"{models}/{relative}") else ""
    selection = model_selection(repo, exists, overrides)
    values.update({key: value for key, value in selection.items() if key.startswith("FARSIGHT_")})
    values["FARSIGHT_CAMERA"] = "" if camera_source == "phone" else "/dev/video0"
    piper_bin = f"{repo}/jetson/.venv/bin/piper"
    if selection["FARSIGHT_PIPER_MODEL"] and exists(piper_bin):
        values["FARSIGHT_PIPER_BIN"] = piper_bin
    cert, key = f"{tls_dir}/cert.pem", f"{tls_dir}/key.pem"
    both = exists(cert) and exists(key)
    values.update(FARSIGHT_SSL_CERTFILE=cert if both else "", FARSIGHT_SSL_KEYFILE=key if both else "")
    values["FARSIGHT_ASSISTANT_URL"] = ASSISTANT_URL if assistant else ""
    values["FARSIGHT_WHISPER_URL"] = WHISPER_URL if whisper else ""
    values["FARSIGHT_SERIAL_PORT"] = next((f"/dev/{prefix}{i}" for prefix in ("ttyACM", "ttyUSB") for i in range(4)
                                           if exists(f"/dev/{prefix}{i}")), "/dev/ttyACM0")
    if serial_port is not None:
        values["FARSIGHT_SERIAL_PORT"] = serial_port
    lines = []
    for line in example_text.replace(PLACEHOLDER_REPO, repo).splitlines():
        name, sep, _ = line.partition("=")
        lines.append(f"{name}={env_value(values[name])}" if sep and name in values and not line.lstrip().startswith("#") else line)
    present = {line.partition("=")[0] for line in lines if "=" in line and not line.lstrip().startswith("#")}
    for name in ("FARSIGHT_LLM_MODEL", "FARSIGHT_LLM_MMPROJ", "FARSIGHT_WHISPER_SERVER_MODEL"):
        if name not in present:
            lines.append(f"{name}={env_value(values[name])}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--tls-dir", default="/etc/farsight/tls")
    parser.add_argument("--assistant", action="store_true", help="the llama-server unit is installed")
    parser.add_argument("--whisper", action="store_true", help="the whisper-server unit is installed")
    parser.add_argument("--camera-source", choices=("phone", "usb"), default="usb")
    parser.add_argument("--serial-port", help="explicit Uno port; an empty argument disables physical motors")
    parser.add_argument("--env-file", type=Path, help="read explicit existing model configuration; never execute it")
    parser.add_argument("--llm-model")
    parser.add_argument("--llm-projector")
    parser.add_argument("--whisper-model")
    parser.add_argument("--piper-model")
    parser.add_argument("--selection-json", action="store_true")
    parser.add_argument("--unit-template", type=Path)
    parser.add_argument("--src", type=Path)
    parser.add_argument("--user")
    args = parser.parse_args(argv)
    overrides = {}
    if args.env_file and args.env_file.is_file():
        from .launch import read_env_file
        overrides.update(read_env_file(args.env_file))
    overrides.update({key: value for key, value in os.environ.items() if key.startswith("FARSIGHT_") and value})
    for key, value in (("FARSIGHT_LLM_MODEL", args.llm_model), ("FARSIGHT_LLM_MMPROJ", args.llm_projector),
                       ("FARSIGHT_WHISPER_MODEL", args.whisper_model), ("FARSIGHT_PIPER_MODEL", args.piper_model)):
        if value:
            overrides[key] = value
            if key == "FARSIGHT_WHISPER_MODEL":
                overrides["FARSIGHT_WHISPER_SERVER_MODEL"] = value
    selection = model_selection(str(args.repo.resolve()), overrides=overrides)
    if args.selection_json:
        if args.env_file and args.env_file.is_file():
            from .launch import read_env_file
            current = read_env_file(args.env_file)
            selection["existing_camera_source"] = "usb" if current.get("FARSIGHT_CAMERA") else "phone"
            selection["existing_serial_port"] = current.get("FARSIGHT_SERIAL_PORT", "")
        selection["serial_override_requested"] = args.serial_port is not None
        selection["requested_serial_port"] = args.serial_port
        sys.stdout.write(json.dumps(selection) + "\n")
        return
    if args.unit_template:
        if not args.src or not args.user:
            parser.error("--unit-template needs --src and --user")
        sys.stdout.write(render_unit(args.unit_template.read_text(), args.repo.resolve(), args.src.resolve(), args.user,
                                   selection, args.env_file or "/etc/farsight.env"))
        return
    example = Path(__file__).with_name("farsight.env.example").read_text()
    sys.stdout.write(render(example, str(args.repo.resolve()), tls_dir=args.tls_dir,
                            assistant=args.assistant, whisper=args.whisper, camera_source=args.camera_source,
                            serial_port=args.serial_port, overrides=overrides))


if __name__ == "__main__":
    main()
