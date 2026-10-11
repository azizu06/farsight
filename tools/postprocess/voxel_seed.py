#!/usr/bin/env python3
"""Merge measured xyz/RGB points in metric voxels; preserve occupied-space coverage."""
import argparse
import json
from pathlib import Path
import time
import numpy as np


def downsample(source, destination, voxel):
    started = time.monotonic()
    dtype = np.dtype([(k, '<f4') for k in ('x', 'y', 'z')] + [(k, 'u1') for k in ('red', 'green', 'blue')])
    with source.open('rb') as f:
        header = []
        for line in f:
            header.append(line)
            if line == b'end_header\n':
                break
        count = next(int(line.split()[-1]) for line in header if line.startswith(b'element vertex'))
        points = np.fromfile(f, dtype=dtype, count=count)
    xyz = np.column_stack([points[k] for k in ('x', 'y', 'z')])
    _, inverse, counts = np.unique(np.floor(xyz / voxel).astype(np.int32), axis=0,
                                   return_inverse=True, return_counts=True)
    out = np.empty(len(counts), dtype=dtype)
    for key in dtype.names:
        means = np.bincount(inverse, weights=points[key], minlength=len(counts)) / counts
        out[key] = np.rint(means) if key in ('red', 'green', 'blue') else means
    updated = [f'element vertex {len(out)}\n'.encode() if line.startswith(b'element vertex') else line for line in header]
    with destination.open('wb') as f:
        f.write(b''.join(updated)); out.tofile(f)
    return dict(input_points=count, output_points=len(out), voxel_m=voxel,
                seconds=time.monotonic() - started)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--voxel', type=float, required=True)
    args = parser.parse_args()
    print(json.dumps(downsample(args.source, args.destination, args.voxel)))


if __name__ == '__main__':
    main()
