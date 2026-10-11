#!/usr/bin/env python3
"""Measured, serial local-data Brush trials sharing the armed watcher's GPU lock."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.postprocess.after_run import BRUSH, command, exclusive, eval_images, trainer_env


def main():
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', type=Path)
    parser.add_argument('--brush', type=Path, default=BRUSH)
    parser.add_argument('--kind', choices=['stable', 'turbo'], default='stable')
    parser.add_argument('--steps', type=int, nargs='+', default=[2000, 3000, 5000])
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--work-dir', type=Path, default=ROOT / '.cache/autosplat')
    parser.add_argument('--between-seconds', type=int, default=35,
                        help='leave a full watcher observation window between GPU trials')
    parser.add_argument('--max-splats', type=int, default=300000)
    parser.add_argument('--extra', nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args()
    args.dataset = args.dataset.resolve()
    args.out = args.out.resolve()
    args.work_dir = args.work_dir.resolve()
    env = trainer_env(args.work_dir)
    for index, steps in enumerate(args.steps):
        output = args.out / str(steps)
        output.mkdir(parents=True, exist_ok=True)
        argv = ['nice', '-n', '10', str(args.brush.resolve()), str(args.dataset),
                '--total-steps' if args.kind == 'stable' else '--total-train-iters', str(steps),
                '--export-every', str(steps), '--export-path', str(output),
                '--max-resolution', '1600', '--max-splats', str(args.max_splats),
                '--eval-split-every', '8', '--eval-every', str(steps), '--eval-save-to-disk',
                *args.extra]
        receipt = dict(dataset=str(args.dataset), kind=args.kind, steps=steps, command=argv,
                       started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        # A benchmark waits for a running automatic job. The watcher continues inventory polls.
        with exclusive(args.work_dir, blocking=True):
            start = time.monotonic()
            with (output / 'brush.log').open('w') as log:
                result = subprocess.run(argv, cwd=args.work_dir / 'brush', env=env,
                                        stdout=log, stderr=subprocess.STDOUT)
            receipt.update(train_s=time.monotonic() - start, exit_code=result.returncode)
            receipt['steps_per_second_wall'] = steps / receipt['train_s']
            # Preserve the measured trainer stage even if a later scorer fails.
            (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
            evaluations = sorted(output.glob('eval_*'))
            if result.returncode == 0 and evaluations:
                scored = command([sys.executable, ROOT / 'tools/postprocess/score_heldout.py',
                                  args.dataset / 'images', eval_images(evaluations[-1])], capture_output=True, text=True)
                receipt['heldout'] = scored.stdout.strip()
            receipt['total_s'] = time.monotonic() - start
        (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt), flush=True)
        if result.returncode:
            raise SystemExit(result.returncode)
        if index + 1 < len(args.steps):
            time.sleep(args.between_seconds)


if __name__ == '__main__':
    main()
