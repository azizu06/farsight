#!/usr/bin/env python3
"""Time saved-run length trials with an explicitly labelled local rsync stand-in."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.postprocess.after_run import exclusive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--durations', type=float, nargs='+', default=[0, 60, 120], help='seconds from first photo; 0 means whole run')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--steps', type=int, default=7500)
    parser.add_argument('--seed-max-points', type=int, default=300000)
    parser.add_argument('--work-dir', type=Path, default=ROOT / '.cache/autosplat')
    args = parser.parse_args()
    args.run, args.out, args.work_dir = args.run.resolve(), args.out.resolve(), args.work_dir.resolve()
    photos = args.run if (args.run / 'transforms.json').is_file() else args.run / 'photos'
    transforms = json.loads((photos / 'transforms.json').read_text())
    frames = transforms['frames']
    start = frames[0].get('t_capture', 0)
    seed = args.run / ('init.ply' if (args.run / 'init.ply').is_file() else 'dense.ply')
    args.out.mkdir(parents=True)
    for duration in args.durations:
        label = f'{duration:g}s' if duration else 'full'
        name = args.run.name + '-cap-' + label
        source, target = args.out / ('source-' + label), args.out / ('pulled-' + label)
        source.mkdir(exist_ok=True)
        selected = [frame for i, frame in enumerate(frames)
                    if not duration or frame.get('t_capture', i / 2.5) - start <= duration]
        for frame in selected:
            destination = source / frame['file_path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                os.link(photos / frame['file_path'], destination)
        (source / 'transforms.json').write_text(json.dumps(dict(transforms, frames=selected)))
        if not (source / 'init.ply').exists():
            os.link(seed, source / 'init.ply')
        target.mkdir(exist_ok=True)
        with exclusive(args.work_dir, blocking=True):
            started = time.monotonic()
            subprocess.run(['rsync', '-a', str(source) + '/', str(target) + '/'], check=True)
            pull_s = time.monotonic() - started
        argv = [sys.executable, '-u', str(ROOT / 'tools/postprocess/after_run.py'), name,
                '--local', str(target), '--work-dir', str(args.work_dir), '--steps', str(args.steps),
                '--seed-max-points', str(args.seed_max_points), '--train-sh-degree', '3', '--skip-quest', '--wait-for-gpu']
        with (args.out / (label + '.log')).open('w') as log:
            result = subprocess.run(argv, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        receipts = sorted((args.work_dir / 'runs' / name).glob('*/timing-receipt.json'))
        receipt = json.loads(receipts[-1].read_text())
        receipt.update(standin_pull_s=pull_s, pull_transport='local rsync stand-in, not Jetson/tether',
                       input_duration_s=selected[-1].get('t_capture', (len(selected) - 1) / 2.5) - start,
                       frames=len(selected), seed_scope='whole saved run, including for prefix trials',
                       end_to_end_ready_s=pull_s + receipt.get('desktop_ready_s', 0))
        (args.out / (label + '-receipt.json')).write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt), flush=True)
        if result.returncode:
            raise SystemExit(result.returncode)
        time.sleep(12)


if __name__ == '__main__':
    main()
