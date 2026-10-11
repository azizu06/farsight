#!/usr/bin/env python3
"""Build Sai's pinned splat-turbo in an ignored cache, without modifying his lane."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path(__file__).resolve().parents[2]
REVISION = '34d2d416dfda207fdd89fd58ba58fd2bca154eb9'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', default=REVISION)
    parser.add_argument('--cache', type=Path, default=ROOT / '.cache/autosplat')
    parser.add_argument('--cargo', default=shutil.which('cargo') or str(
        Path.home() / '.rustup/toolchains/stable-aarch64-apple-darwin/bin/cargo'))
    args = parser.parse_args()
    cache = args.cache.resolve()
    source = cache / ('turbo-' + args.revision[:12])
    source.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(['git', 'archive', args.revision, 'splat'], cwd=ROOT,
                             check=True, capture_output=True).stdout
    subprocess.run(['tar', '-x', '-C', str(source)], input=archive, check=True)
    workspace = source / 'splat/brush'
    target = cache / 'turbo-target'
    env = dict(os.environ, CARGO_HOME=str(cache / 'cargo-home'), CARGO_TARGET_DIR=str(target))
    env['PATH'] = str(Path(shutil.which(args.cargo) or args.cargo).resolve().parent) + os.pathsep + env.get('PATH', '')
    started = time.monotonic()
    argv = ['nice', '-n', '19', args.cargo, 'build', '--release', '-p', 'brush-cli',
            '-p', 'farsight-splat', '-p', 'farsight-seed', '--bins',
            '--features', 'brush-cli/metal-msl']
    subprocess.run(argv, cwd=workspace, env=env, check=True)
    receipt = {'revision': args.revision, 'build_s': time.monotonic() - started,
               'source': str(source), 'target': str(target), 'command': argv}
    (cache / 'turbo-build-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
