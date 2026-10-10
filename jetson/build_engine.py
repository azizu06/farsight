"""Build TensorRT engines from ONNX with trtexec, cached by ONNX hash. Run on the Jetson.

    python -m jetson.build_engine models/yolo11n-pose.onnx

Writes models/yolo11n-pose.engine plus a .engine.json stamp (ONNX sha256, TensorRT
version, precision, shapes). A rerun with the same stamp skips trtexec, so it is instant.
An engine only runs on the TensorRT version and GPU that built it; a TensorRT upgrade rebuilds.
"""

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

DEFAULT_TRTEXEC = "/usr/src/tensorrt/bin/trtexec"


class BuildError(RuntimeError):
    pass


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tensorrt_version():
    try:
        return importlib.import_module("tensorrt").__version__
    except ImportError:
        return "unknown"


def find_trtexec():
    return shutil.which("trtexec") or DEFAULT_TRTEXEC


def build_engine(onnx, engine=None, *, trtexec=None, fp16=True, shapes=None, force=False,
                 run=subprocess.run, trt_version=None):
    """Return (engine path, built). shapes ("images:1x3x640x640") is only for dynamic-shape ONNX."""
    onnx = Path(onnx)
    engine = Path(engine) if engine else onnx.with_suffix(".engine")
    trtexec = Path(trtexec or find_trtexec())
    if not onnx.is_file():
        raise BuildError(f"ONNX model not found: {onnx}")
    stamp_path = engine.with_name(engine.name + ".json")
    stamp = dict(onnx_sha256=sha256(onnx), tensorrt=trt_version or tensorrt_version(), fp16=fp16, shapes=shapes)
    if not force and engine.is_file():
        try:
            if json.loads(stamp_path.read_text()) == stamp:
                return engine, False
        except (OSError, ValueError):
            pass
    if not trtexec.is_file():
        raise BuildError(f"trtexec not found at {trtexec}; install TensorRT (apt install nvidia-jetpack)")
    partial = engine.with_name(engine.name + ".partial")
    command = [str(trtexec), f"--onnx={onnx}", f"--saveEngine={partial}", "--memPoolSize=workspace:512",
               "--skipInference"]
    if fp16:
        command.append("--fp16")
    if shapes:
        command += [f"--minShapes={shapes}", f"--optShapes={shapes}", f"--maxShapes={shapes}"]
    stamp_path.unlink(missing_ok=True)
    try:
        result = run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if result.returncode != 0 or not partial.is_file() or partial.stat().st_size == 0:
            tail = "\n".join((result.stdout or "").splitlines()[-15:])
            raise BuildError(f"trtexec failed for {onnx.name} (exit {result.returncode}):\n{tail}")
        os.replace(partial, engine)
    finally:
        partial.unlink(missing_ok=True)
    stamp_path.write_text(json.dumps(stamp, indent=2) + "\n")
    return engine, True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("onnx", nargs="+", type=Path)
    parser.add_argument("--trtexec", type=Path, help=f"default: trtexec on PATH, else {DEFAULT_TRTEXEC}")
    parser.add_argument("--no-fp16", dest="fp16", action="store_false", help="build FP32 (default FP16)")
    parser.add_argument("--shapes", help="dynamic-shape ONNX only, e.g. images:1x3x640x640")
    parser.add_argument("--force", action="store_true", help="rebuild even when the cache stamp matches")
    args = parser.parse_args(argv)
    failed = False
    for onnx in args.onnx:
        started = time.monotonic()
        try:
            engine, built = build_engine(onnx, trtexec=args.trtexec, fp16=args.fp16, shapes=args.shapes,
                                         force=args.force)
        except BuildError as exc:
            failed = True
            print(f"FAILED {onnx}: {exc}", file=sys.stderr)
            continue
        print(f"{'built' if built else 'cached'} {engine} ({time.monotonic() - started:.1f}s)")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
