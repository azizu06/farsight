"""Prepare detector artifacts on a laptop before the offline hardware setup."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", default="yolo11n.pt", help="Official detection weights or a local checkpoint")
    parser.add_argument("--output-dir", type=Path, default=Path("models"))
    args = parser.parse_args()
    try:
        import ultralytics
        from ultralytics import YOLO
    except ImportError:
        parser.error("Install laptop export dependencies: python -m pip install ultralytics onnx")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    weights = Path(args.weights)
    # An official model name is downloaded by Ultralytics into our model folder.
    if not weights.exists() and weights.parent == Path("."):
        weights = output / weights.name
    model = YOLO(str(weights))
    if model.task not in ("detect", "pose"):
        parser.error("Use detection or pose weights, not segmentation/classification weights")
    exported = Path(model.export(format="onnx", imgsz=640, opset=12, simplify=False,
                                 dynamic=False, nms=False, device="cpu"))
    target = output / exported.name
    if exported.resolve() != target:
        shutil.copyfile(exported, target)
    names = model.names
    labels = [names[i] for i in range(len(names))]
    target.with_suffix(".labels.json").write_text(json.dumps(labels, indent=2) + "\n")
    manifest = dict(model=target.name, sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                    bytes=target.stat().st_size, input_shape=[1, 3, 640, 640],
                    task=model.task,
                    output_contract=("YOLO person xywh + score + 17 xyz confidence keypoints; external NMS"
                                     if model.task == "pose" else "YOLO detection xywh plus COCO class scores; external NMS"),
                    classes=len(labels), opset=12, dynamic=False, nms=False,
                    exporter=f"ultralytics {ultralytics.__version__}",
                    source="https://github.com/ultralytics/assets", license="AGPL-3.0 or applicable Ultralytics license")
    target.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    print("Copy models to the Jetson, then build its TensorRT engine on that device.")


if __name__ == "__main__":
    main()
