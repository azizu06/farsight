#!/usr/bin/env python3
"""Install pinned OpenCV face models and licenses without changing the runtime."""

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shlex
import sys
import tempfile
from urllib.request import Request, urlopen


REVISION = "47534e27c9851bb1128ccc0102f1145e27f23f98"
MEDIA = f"https://media.githubusercontent.com/media/opencv/opencv_zoo/{REVISION}/models"
RAW = f"https://raw.githubusercontent.com/opencv/opencv_zoo/{REVISION}/models"
CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class FileSpec:
    filename: str
    url: str
    size: int
    sha256: str


FILES = (
    FileSpec("face_detection_yunet_2023mar.onnx",
             f"{MEDIA}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
             232589, "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    FileSpec("face_recognition_sface_2021dec.onnx",
             f"{MEDIA}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
             38696353, "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
    FileSpec("YuNet-LICENSE.txt", f"{RAW}/face_detection_yunet/LICENSE",
             1085, "c83b8120c50ccbd4c4f96edf53141bdd566ebb8f8e9227e415326aa1b1aba958"),
    FileSpec("SFace-LICENSE.txt", f"{RAW}/face_recognition_sface/LICENSE",
             11358, "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"),
)


def _matches(path: Path, spec: FileSpec) -> bool:
    try:
        if path.stat().st_size != spec.size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as source:
            remaining = spec.size
            while remaining:
                block = source.read(min(CHUNK_SIZE, remaining))
                if not block:
                    return False
                remaining -= len(block)
                digest.update(block)
            if source.read(1):
                return False
        return digest.hexdigest() == spec.sha256
    except (FileNotFoundError, IsADirectoryError):
        return False


def download_verified(spec: FileSpec, destination: Path, timeout: float = 30) -> Path:
    """Publish only an exact size/hash match; retain any old file on failure."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / spec.filename
    if _matches(target, spec):
        return target

    partial = None
    try:
        request = Request(spec.url, headers={"User-Agent": "Farsight-model-setup/1"})
        with urlopen(request, timeout=timeout) as response:
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != spec.size:
                raise ValueError(f"{spec.filename}: download size differs from pinned {spec.size} bytes")
            with tempfile.NamedTemporaryFile(prefix=f".{spec.filename}.", suffix=".part",
                                             dir=destination, delete=False) as output:
                partial = Path(output.name)
                digest = hashlib.sha256()
                received = 0
                while True:
                    # Read one byte beyond the bound to reject an oversized stream.
                    block = response.read(min(CHUNK_SIZE, spec.size + 1 - received))
                    if not block:
                        break
                    received += len(block)
                    if received > spec.size:
                        raise ValueError(f"{spec.filename}: download exceeds pinned size")
                    digest.update(block)
                    output.write(block)
                if received != spec.size:
                    raise ValueError(f"{spec.filename}: download size {received}, expected {spec.size}")
                if digest.hexdigest() != spec.sha256:
                    raise ValueError(f"{spec.filename}: SHA-256 verification failed")
                output.flush()
                os.fsync(output.fileno())
        os.replace(partial, target)
        partial = None
        return target
    finally:
        if partial is not None:
            partial.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path,
                        default=Path(__file__).resolve().parents[1] / "models" / "identity",
                        help="model/license directory (default: repository models/identity)")
    args = parser.parse_args(argv)
    directory = args.destination.expanduser().resolve()
    try:
        for spec in FILES:
            path = download_verified(spec, directory)
            print(f"Verified {path.name} ({spec.size:,} bytes)")
    except (OSError, ValueError) as error:
        print(f"Face model setup failed: {error}", file=sys.stderr)
        return 1
    print("\nModels installed. Enable recognition in the rover process with:")
    print("export FARSIGHT_IDENTITY_YUNET=" + shlex.quote(str(directory / FILES[0].filename)))
    print("export FARSIGHT_IDENTITY_SFACE=" + shlex.quote(str(directory / FILES[1].filename)))
    print("See docs/face-identity.md before restarting the rover. No service was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
