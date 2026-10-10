"""Render /etc/farsight.env from jetson/farsight.env.example for the files that exist on this Jetson.

jetson/install_service.sh runs this once (stdlib only, so it works before the venv exists):
    python3 -m jetson.envgen --repo ~/farsight [--tls-dir /etc/farsight/tls] [--assistant] [--whisper]
It prefers the TensorRT pose engine, then the ONNX pose model, then the bundled HOG detector, and only turns on
optional features (face model, Piper voices, HTTPS, llama/whisper URLs) when their files or units exist, so the
first boot never points at something missing.
"""

import argparse
import os
import sys
from pathlib import Path

PLACEHOLDER_REPO = "/home/farsight/farsight"
ASSISTANT_URL = "http://127.0.0.1:8092"
WHISPER_URL = "http://127.0.0.1:8091"


def render(example_text, repo, exists=os.path.exists, tls_dir="/etc/farsight/tls", assistant=False, whisper=False):
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
    for key, relative in (("FARSIGHT_FACE_MODEL", "version-RFB-320.onnx"),
                          ("FARSIGHT_PIPER_MODEL", "piper/en_US-lessac-medium.onnx"),
                          ("FARSIGHT_PIPER_MODEL_ES", "piper/es_MX-claude-high.onnx")):
        values[key] = f"{models}/{relative}" if exists(f"{models}/{relative}") else ""
    cert, key = f"{tls_dir}/cert.pem", f"{tls_dir}/key.pem"
    both = exists(cert) and exists(key)
    values.update(FARSIGHT_SSL_CERTFILE=cert if both else "", FARSIGHT_SSL_KEYFILE=key if both else "")
    values["FARSIGHT_ASSISTANT_URL"] = ASSISTANT_URL if assistant else ""
    values["FARSIGHT_WHISPER_URL"] = WHISPER_URL if whisper else ""
    values["FARSIGHT_SERIAL_PORT"] = next((f"/dev/{prefix}{i}" for prefix in ("ttyACM", "ttyUSB") for i in range(4)
                                           if exists(f"/dev/{prefix}{i}")), "/dev/ttyACM0")
    lines = []
    for line in example_text.replace(PLACEHOLDER_REPO, repo).splitlines():
        name, sep, _ = line.partition("=")
        lines.append(f"{name}={values[name]}" if sep and name in values and not line.lstrip().startswith("#") else line)
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--tls-dir", default="/etc/farsight/tls")
    parser.add_argument("--assistant", action="store_true", help="the llama-server unit is installed")
    parser.add_argument("--whisper", action="store_true", help="the whisper-server unit is installed")
    args = parser.parse_args(argv)
    example = Path(__file__).with_name("farsight.env.example").read_text()
    sys.stdout.write(render(example, str(args.repo.resolve()), tls_dir=args.tls_dir,
                            assistant=args.assistant, whisper=args.whisper))


if __name__ == "__main__":
    main()
