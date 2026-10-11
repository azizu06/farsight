"""Synthetic CPU-only map-pull lock benchmark; never contacts or drives a rover.

python -m tools.benchmark_voxel_snapshot --points 120000 --max-locked-ms 10
For a read-only remote trial, pipe voxel.py to this script with --voxel-source -.
"""

import argparse
import json
from pathlib import Path
import platform
import sys
import threading
import time

import numpy as np


def benchmark(cloud_type, points=120_000, repeats=30):
    cloud = cloud_type(capacity=points)
    # Unique voxels in a compact grid, then touch every seventh voxel to exercise
    # LRU ordering rather than measuring an untouched, already-sorted buffer.
    i = np.arange(points)
    xyz = np.column_stack((i % 100, i // 100 % 100, i // 10000)) * .08 + .01
    rgb = np.column_stack((i % 256, i * 3 % 256, i * 7 % 256)).astype(np.uint8)
    cloud.integrate(xyz, rgb)
    cloud.integrate(xyz[::7] + .001, rgb[::7])
    lock = threading.RLock()

    def legacy_snapshot():
        values = list(cloud.cells.values())
        return (np.array([v[0] for v in values], np.float32),
                np.clip([v[1] for v in values], 0, 255).astype(np.uint8))

    expected = legacy_snapshot()
    actual = cloud.snapshot()
    for old, new in zip(expected, actual):
        np.testing.assert_array_equal(old, new)
    timings = {}
    for name, snapshot in (("legacy_python", legacy_snapshot), ("snapshot", cloud.snapshot)):
        samples = []
        for _ in range(repeats):
            started = time.perf_counter()
            with lock:
                snapshot()
            samples.append((time.perf_counter() - started) * 1000)
        timings[name] = dict(p50=float(np.median(samples)), p95=float(np.percentile(samples, 95)),
                             max=max(samples))
    return dict(scope="synthetic CPU-only snapshot under map lock; not live AUTO qualification",
                platform=platform.platform(), python=platform.python_version(), numpy=np.__version__,
                points=len(cloud.cells), repeats=repeats, locked_ms=timings, exact_snapshot_parity=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", type=int, default=120_000)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--max-locked-ms", type=float)
    parser.add_argument("--voxel-source", help="voxel.py path, or - for stdin; loaded only in memory")
    args = parser.parse_args()
    if args.voxel_source:
        source = sys.stdin.read() if args.voxel_source == "-" else Path(args.voxel_source).read_text()
        namespace = {}
        exec(compile(source, "voxel.py", "exec"), namespace)
        cloud_type = namespace["VoxelMap"]
    else:
        from jetson.mapping.voxel import VoxelMap
        cloud_type = VoxelMap
    result = benchmark(cloud_type, args.points, args.repeats)
    print(json.dumps(result, indent=2))
    if args.max_locked_ms is not None and result["locked_ms"]["snapshot"]["max"] >= args.max_locked_ms:
        sys.exit(1)
