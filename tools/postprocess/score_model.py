#!/usr/bin/env python3
"""Evaluate an exported PLY by reloading it with frozen learning rates.

The receipt reports reload/export differences so this cannot silently score a
newly trained replacement for the requested model. Shares the watcher GPU lock.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.postprocess.after_run import BRUSH, exclusive, trainer_env
from tools.splat.prune import read_ply, sh_degree


def main():
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', type=Path)
    parser.add_argument('dataset', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--brush', type=Path, default=BRUSH)
    parser.add_argument('--work-dir', type=Path, default=ROOT / '.cache/autosplat')
    args = parser.parse_args()
    model, dataset, output = args.model.resolve(), args.dataset.resolve(), args.out.resolve()
    output.mkdir(parents=True)
    prepared = output / 'dataset'
    prepared.mkdir()
    os.symlink(dataset / 'images', prepared / 'images', target_is_directory=True)
    shutil.copyfile(model, prepared / 'init.ply')
    transforms = json.loads((dataset / 'transforms.json').read_text())
    transforms['ply_file_path'] = 'init.ply'
    (prepared / 'transforms.json').write_text(json.dumps(transforms))
    _, names, before = read_ply(model)
    exports = output / 'exports'
    exports.mkdir()
    # Tiny positive endpoints avoid 0/0 in the exponential LR schedules.
    argv = ['nice', '-n', '10', str(args.brush.resolve()), str(prepared),
            '--total-steps', '1', '--export-every', '1', '--export-path', str(exports),
            '--sh-degree', str(sh_degree(names)), '--max-splats', str(len(before)),
            '--max-resolution', '1600', '--eval-split-every', '8', '--eval-every', '1',
            '--eval-save-to-disk', '--lr-mean', '1e-30', '--lr-mean-end', '1e-30',
            '--mean-noise-weight', '0', '--lr-scale', '1e-30', '--lr-scale-end', '1e-30',
            '--lr-coeffs-dc', '0', '--lr-opac', '0', '--lr-rotation', '0']
    with exclusive(args.work_dir.resolve(), blocking=True):
        started = time.monotonic()
        with (output / 'brush.log').open('w') as log:
            subprocess.run(argv, cwd=args.work_dir.resolve() / 'brush',
                           env=trainer_env(args.work_dir.resolve()),
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        _, after_names, after = read_ply(sorted(exports.glob('*.ply'))[-1])
        common = [name for name in names if name in after_names]
        differences = {name: float(np.max(np.abs(before[:, names.index(name)] -
                                                after[:, after_names.index(name)])))
                       for name in common} if len(before) == len(after) else {}
        score = subprocess.run([sys.executable, str(ROOT / 'tools/postprocess/score_heldout.py'),
                                str(dataset / 'images'), str(exports / 'eval_1')],
                               check=True, capture_output=True, text=True).stdout.strip()
        receipt = dict(model=str(model), dataset=str(dataset), command=argv,
                       seconds=time.monotonic() - started, input_splats=len(before),
                       output_splats=len(after), missing_properties=sorted(set(names) - set(after_names)),
                       max_abs_difference_by_property=differences, heldout=score,
                       method='one-step reload with frozen learning rates; inspect numerical differences')
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt), flush=True)


if __name__ == '__main__':
    main()
