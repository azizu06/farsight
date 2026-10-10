"""Download the models that can be fetched, into models/ (not in git). Idempotent: present files are skipped.

python3 -m tools.fetch_models --check                 # what is present / missing, no network
python3 -m tools.fetch_models                         # piper voices (English + Spanish), face model, whisper base.en
python3 -m tools.fetch_models --groups vlm-qwen3      # Qwen3-VL-2B Q4_K_M + mmproj (~1.55 GB, default assistant model)
python3 -m tools.fetch_models --groups vlm            # Qwen2.5-VL-3B Q4 + mmproj (~2.8 GB, older fallback)
python3 -m tools.fetch_models --dry-run

The YOLO ONNX files cannot be downloaded: export them on a laptop (group `export`, checked but never fetched).
Stdlib only, so jetson/setup.sh can run it on the Jetson too. Downloads land in `<name>.part` and are renamed only
after the size (and SHA-256 where pinned) matches, so an interrupted run never leaves a corrupt model behind.
"""

import argparse
import hashlib
import os
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIPER = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
QWEN = "https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF/resolve/main"
QWEN3 = "https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct-GGUF/resolve/main"
FACE_REV = "6fd293d22b523ec88959f104b8eef5395e3adfbc"


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Item:
    name: str  # path under models/
    url: str | None = None
    sha256: str | None = None
    hint: str = ""


def _voice(path):
    stem = path.rsplit("/", 1)[1]
    return [Item(f"piper/{stem}.{ext}", f"{PIPER}/{path}.{ext}") for ext in ("onnx", "onnx.json")]


GROUPS = {
    "piper": _voice("en/en_US/lessac/medium/en_US-lessac-medium") + _voice("es/es_MX/claude/high/es_MX-claude-high"),
    "face": [Item("version-RFB-320.onnx", f"https://huggingface.co/onnxmodelzoo/version-RFB-320/resolve/{FACE_REV}/version-RFB-320.onnx",
                  sha256="34cd7e60aeff28744c657de7a3dc64e872d506741de66987f3426f2b79f88017")],
    "whisper": [Item("ggml-base.en.bin", "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin")],
    "vlm": [Item("Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf", f"{QWEN}/Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"),
            Item("mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf", f"{QWEN}/mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf")],
    "vlm-qwen3": [Item("Qwen3VL-2B-Instruct-Q4_K_M.gguf", f"{QWEN3}/Qwen3VL-2B-Instruct-Q4_K_M.gguf",
                       sha256="089d75c52f4b7ffc56ba998ffc50aae89fcafc755f9e7208aacca281dca6c2ae"),
                  Item("mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf", f"{QWEN3}/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf",
                       sha256="f9a68fabba69c3b81e153367b2c7521030b0fa8bb0de400c9599c8e6725f9c82")],
    "export": [Item("yolo11n-pose.onnx", hint="python3 -m tools.export_detector --weights yolo11n-pose.pt"),
               Item("yolo11n.onnx", hint="python3 -m tools.export_detector --weights yolo11n.pt")],
}
DEFAULT_GROUPS = ("piper", "face", "whisper")


def fetch_file(item, models_dir, opener=urllib.request.urlopen):
    target = Path(models_dir) / item.name
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    try:
        with opener(item.url, timeout=60) as response, open(part, "wb") as f:
            expected = response.headers.get("Content-Length")
            written = 0
            while chunk := response.read(1 << 20):
                f.write(chunk)
                digest.update(chunk)
                written += len(chunk)
        if written == 0:
            raise FetchError(f"{item.name}: empty download from {item.url}")
        if expected is not None and int(expected) != written:
            raise FetchError(f"{item.name}: got {written} of {expected} bytes (connection dropped?)")
        if item.sha256 and digest.hexdigest() != item.sha256:
            raise FetchError(f"{item.name}: SHA-256 {digest.hexdigest()} != pinned {item.sha256}")
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return written


def main(argv=None, opener=urllib.request.urlopen, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--groups", default=",".join(DEFAULT_GROUPS), help=f"comma list of: {', '.join(GROUPS)}")
    parser.add_argument("--models-dir", default=str(ROOT / "models"))
    parser.add_argument("--check", action="store_true", help="Report present/missing and exit 1 if anything is missing; no network")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be downloaded")
    args = parser.parse_args(argv)
    groups = [g.strip() for g in args.groups.split(",") if g.strip()]
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        parser.error(f"unknown group {', '.join(unknown)} (choose from {', '.join(GROUPS)})")
    failed = missing = 0
    for group in groups:
        for item in GROUPS[group]:
            target = Path(args.models_dir) / item.name
            if target.is_file() and target.stat().st_size > 0:
                print(f"present   {item.name}", file=out)
            elif item.url is None:
                missing += 1
                print(f"MISSING   {item.name}   build it on a laptop: {item.hint}", file=out)
            elif args.check:
                missing += 1
                print(f"MISSING   {item.name}", file=out)
            elif args.dry_run:
                print(f"would download {item.name} <- {item.url}", file=out)
            else:
                print(f"download  {item.name} ...", file=out, flush=True)
                try:
                    size = fetch_file(item, args.models_dir, opener)
                    print(f"          {size / 1e6:.1f} MB ok", file=out)
                except (OSError, FetchError) as exc:
                    failed += 1
                    print(f"FAILED    {item.name}: {exc}", file=out)
    return 1 if failed or missing else 0


if __name__ == "__main__":
    sys.exit(main())
