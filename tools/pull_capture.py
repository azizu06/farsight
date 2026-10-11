#!/usr/bin/env python3
"""Copy the newest completed phone dataset from the Jetson after a run."""

import argparse
from pathlib import Path
import shlex
import subprocess


# Run IDs sort by UTC creation time. A published transforms.json means at least
# one complete image/pose pair, even if the Jetson later loses power.
LATEST = """from pathlib import Path
runs = sorted(Path.home().joinpath('captures').glob('*/transforms.json'))
if not runs:
    raise SystemExit('No captured runs in ~/captures')
print(runs[-1].parent)
"""


def pull_latest_run(host, destination):
    remote = subprocess.check_output(
        ['ssh', host, 'python3 -c ' + shlex.quote(LATEST)], text=True).strip()
    destination = Path(destination).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(['scp', '-r', f'{host}:{remote}', str(destination)], check=True)
    return destination / Path(remote).name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='ubuntu@100.103.169.40', help='Jetson SSH host')
    parser.add_argument('--destination', default='~/farsight-captures/', help='Mac dataset directory')
    args = parser.parse_args()
    print(pull_latest_run(args.host, args.destination))


if __name__ == '__main__':
    main()
