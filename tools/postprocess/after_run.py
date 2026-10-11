#!/usr/bin/env python3
"""Mac post-run Brush pipeline. See AFTER_RUN.md for commands and measured budgets."""
import argparse
import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
BRUSH = Path.home() / '.cache/farsight-splat/brush-v0.3.0/brush-app-aarch64-apple-darwin/brush_app'
DIST = Path('/Users/aziz.u/.treehouse/farsight-5a56e1/5/farsight/.cache/050-runtime/quest/dist')
# One small SSH response per observation; no image or point-cloud bytes cross the link.
INVENTORY = '''import json
from pathlib import Path
out = {}
for run in sorted(Path.home().joinpath("captures").iterdir()):
    if not run.is_dir(): continue
    images = list(run.joinpath("images").glob("*.jpg"))
    tf, seed = run / "transforms.json", run / "init.ply"
    files = images + [p for p in (tf, seed) if p.is_file()]
    out[run.name] = [len(images), max((p.stat().st_mtime_ns for p in files), default=0),
                     seed.stat().st_size if seed.is_file() else 0,
                     tf.stat().st_size if tf.is_file() else 0]
print(json.dumps(out))
'''


def command(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def inventory(host):
    result = command(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', host,
                      'python3 -c ' + shlex.quote(INVENTORY)], capture_output=True, text=True)
    return json.loads(result.stdout)


def local_runs(base):
    return {p.name: p for p in sorted(base.glob('*'))
            if (p / 'init.ply').is_file() and
            ((p / 'transforms.json').is_file() or (p / 'photos/transforms.json').is_file())}


def saved_runs(args):
    return {**local_runs(args.local_root), **local_runs(args.work_dir / 'captures')}


def finished(previous, current, now, stable_seconds=60):
    """Track capture signatures. Any active capture defers ALL remote pulls."""
    observations = {}
    for name, signature in current.items():
        old = previous.get(name)
        since = old[1] if old and old[0] == signature else now
        observations[name] = (signature, since)
    ready = [name for name, (_, since) in observations.items() if now - since >= stable_seconds]
    idle = len(ready) == len(current)
    return observations, [name for name in ready if current[name][2] and current[name][3]] if idle else []


def wait_for_run(args):
    observations = {}
    started = time.monotonic()
    while True:
        try:
            current = inventory(args.host)
        except subprocess.CalledProcessError:
            runs = saved_runs(args)
            name = args.run_id or (max(runs) if runs else None)
            if name in runs:
                print('Jetson unreachable; using saved LOCAL run, no pull.', flush=True)
                return name, runs[name], 'offline-local', 0, time.monotonic() - started
            raise RuntimeError('Jetson unreachable and no complete local run is available')
        observations, ready = finished(observations, current, time.monotonic())
        name = args.run_id or (max(ready) if ready else None)
        if name in ready:
            stage = time.monotonic()
            source = pull(args, name)
            return name, source, 'rsync', time.monotonic() - stage, stage - started
        print('Waiting for all capture image counts/mtimes to stay unchanged for >=60 s.', flush=True)
        time.sleep(args.poll_seconds)


def pull(args, name):
    destination = args.work_dir / 'captures' / name
    destination.mkdir(parents=True, exist_ok=True)
    command(['rsync', '-a', '--bwlimit=' + str(args.bwlimit),
             '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=5',
             '--include=/images/', '--include=/images/*.jpg', '--include=/transforms.json',
             '--include=/init.ply', '--exclude=*',
             f'{args.host}:captures/{name}/', str(destination) + '/'])
    return destination


def prepare(source, destination):
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    dataset = source if (source / 'transforms.json').is_file() else source / 'photos'
    transforms = json.loads((dataset / 'transforms.json').read_text())
    clean = destination / 'decoded'
    clean.mkdir(parents=True)
    frames, rejected = [], []
    for frame in transforms['frames']:
        image = dataset / frame['file_path']
        try:
            with Image.open(image) as decoded:
                decoded.verify()
            with Image.open(image) as decoded:
                decoded.load()
            # Brush v0.3.0 reads a fixed 16,387-byte header before decoding.
            if image.stat().st_size < 16387:
                raise ValueError('short file triggers Brush v0.3.0 early eof')
        except (OSError, ValueError, SyntaxError) as error:
            rejected.append({'file': frame['file_path'], 'reason': str(error)})
            continue
        target = clean / frame['file_path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(image, target)
        frames.append(frame)
    if not frames:
        raise RuntimeError('No decodable images')
    transforms['frames'] = frames
    (clean / 'transforms.json').write_text(json.dumps(transforms))
    drives = command([sys.executable, ROOT / 'tools/postprocess/find_drives.py', clean],
                     capture_output=True, text=True).stdout
    print(drives, flush=True)
    segments = [(int(a), int(b)) for a, b in re.findall(r'frames\s+(\d+)-\s*(\d+)', drives)]
    if not segments:
        raise RuntimeError('No continuous drive found')
    def duration(segment):
        a, b = segment
        return frames[b].get('t_capture', b / 2.5) - frames[a].get('t_capture', a / 2.5)
    a, b = max(segments, key=duration)
    cut = destination / 'drive'
    command([sys.executable, ROOT / 'tools/postprocess/find_drives.py', clean, '--cut', a, b, cut])
    shutil.copy2(source / 'init.ply', cut / 'init.ply')
    selected = json.loads((cut / 'transforms.json').read_text())
    selected['ply_file_path'] = 'init.ply'
    (cut / 'transforms.json').write_text(json.dumps(selected))
    with (cut / 'init.ply').open('rb') as seed:
        for line in seed:
            if line.startswith(b'element vertex '):
                points = int(line.split()[-1])
                break
        else:
            raise RuntimeError('init.ply has no vertex count')
    return cut, {'input_frames': len(frames) + len(rejected), 'rejected': rejected,
                 'drive_frames': b - a + 1, 'drive_duration_s': duration((a, b)), 'seed_points': points}


def publish(splat, dist, name):
    scenes = dist / 'scenes'
    scenes.mkdir(parents=True, exist_ok=True)
    target = scenes / (name + '.ply')
    temporary = scenes / (name + '.ply.tmp')
    shutil.copyfile(splat, temporary)
    os.replace(temporary, target)
    return target


@contextlib.contextmanager
def exclusive(work):
    work.mkdir(parents=True, exist_ok=True)
    with (work / 'pipeline.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Another after_run/watch process owns this work directory')
        yield


def process(args, name, source, pull_s=0, transport='local', wait_s=0):
    started = time.monotonic()
    output = args.work_dir / 'runs' / name / time.strftime('%Y%m%dT%H%M%S')
    output.mkdir(parents=True)
    receipt = {'run_id': name, 'source': str(source), 'transport': transport, 'pull_s': pull_s,
               'steps': args.steps, 'wait_for_end_s': wait_s, 'status': 'running'}
    try:
        stage = time.monotonic()
        dataset, prep = prepare(source, output)
        receipt.update(prep, prep_s=time.monotonic() - stage)
        exports = output / 'exports'
        exports.mkdir()
        work = args.work_dir / 'brush'
        work.mkdir(exist_ok=True)
        # Cap the *initial* seed too: Brush's max-splats only limits later growth.
        subsample = max(1, math.ceil(prep['seed_points'] / 300_000))
        argv = ['nice', '-n', '10', args.brush, dataset, '--total-steps', args.steps,
                '--export-every', args.steps, '--export-path', exports,
                '--max-resolution', args.max_resolution, '--sh-degree', '1',
                '--max-splats', '300000', '--subsample-points', subsample,
                '--eval-split-every', '8', '--eval-every', args.steps, '--eval-save-to-disk']
        receipt['brush_command'] = [str(a) for a in argv]
        stage = time.monotonic()
        with (output / 'brush.log').open('w') as log:
            command(argv, cwd=work, stdout=log, stderr=subprocess.STDOUT)
        receipt['train_s'] = time.monotonic() - stage
        receipt['steps_per_second_wall'] = args.steps / receipt['train_s']
        receipt['suggested_120s_steps'] = max(1, round(args.steps * 120 / receipt['train_s']))
        raw = sorted(exports.glob('*.ply'))[-1]
        stage = time.monotonic()
        pruned = command([sys.executable, '-m', 'tools.splat.prune', raw, output / 'splat.ply',
                          '--max-splats', '300000', '--sh-degree', '1'], cwd=ROOT,
                         capture_output=True, text=True)
        receipt.update(prune_s=time.monotonic() - stage, prune=json.loads(pruned.stdout))
        stage = time.monotonic()
        receipt['published'] = str(publish(output / 'splat.ply', args.dist, name))
        receipt['publish_s'] = time.monotonic() - stage
        receipt['url'] = args.base_url.rstrip('/') + '/splat.html?scene=scenes/' + quote(name) + '.ply&type=ply'
        stage = time.monotonic()
        evaluations = list(exports.glob('eval_*'))
        if evaluations:
            score = command([sys.executable, ROOT / 'tools/postprocess/score_heldout.py',
                             dataset / 'images', evaluations[-1]], capture_output=True, text=True)
            receipt['heldout'] = score.stdout.strip()
        receipt['score_s'] = time.monotonic() - stage
        receipt['status'] = 'complete'
    except (Exception, SystemExit, KeyboardInterrupt) as error:
        receipt.update(status='failed', error=str(error))
        raise
    finally:
        receipt['total_s'] = time.monotonic() - started + pull_s
        (output / 'timing-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        print(json.dumps(receipt, indent=2), flush=True)
    (args.work_dir / (name + '.done.json')).write_text(json.dumps(receipt, indent=2) + '\n')
    return receipt


def watch(args):
    observations = {}
    while True:
        tick = time.monotonic()
        try:
            current = inventory(args.host)
        except subprocess.CalledProcessError:
            # A powered-off Jetson cannot be pulled; complete saved datasets remain usable.
            candidates = [(name, path, 'offline-local') for name, path in saved_runs(args).items()]
        else:
            observations, ready = finished(observations, current, tick)
            candidates = [(name, None, 'rsync') for name in ready]
        for name, source, transport in sorted(candidates, reverse=True):
            if (args.work_dir / (name + '.done.json')).exists():
                continue
            # Process one run per tick; the next SSH observation must confirm the link is idle again.
            try:
                stage = time.monotonic()
                source = source or pull(args, name)
                pull_s = time.monotonic() - stage if transport == 'rsync' else 0
                process(args, name, source, pull_s, transport)
            except Exception as error:
                print(f'{name}: {error}', file=sys.stderr, flush=True)
            break
        time.sleep(max(0, args.poll_seconds - (time.monotonic() - tick)))


def main():
    # Let subprocess.run terminate/reap the active child before releasing the run lock.
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run_id', nargs='?', help='omit for newest finished run')
    parser.add_argument('--watch', action='store_true', help='stay running; one completion receipt per run')
    parser.add_argument('--local', type=Path, help='saved completed run; skips SSH and pull')
    parser.add_argument('--local-root', type=Path, default=Path.home() / 'farsight-captures')
    parser.add_argument('--host', default='jetson-ts')
    parser.add_argument('--work-dir', type=Path, default=ROOT / '.cache/autosplat')
    parser.add_argument('--dist', type=Path, default=DIST)
    parser.add_argument('--base-url', default='http://localhost:8080')
    parser.add_argument('--steps', type=int, default=int(os.environ.get('SPLAT_STEPS', '5000')))
    parser.add_argument('--max-resolution', type=int, default=640)
    parser.add_argument('--brush', type=Path, default=Path(os.environ.get('BRUSH', str(BRUSH))))
    parser.add_argument('--poll-seconds', type=int, choices=range(30, 61), default=30, metavar='30..60')
    parser.add_argument('--bwlimit', type=int, default=2048, help='rsync KiB/s; default 2 MiB/s')
    args = parser.parse_args()
    args.work_dir = args.work_dir.expanduser().resolve()
    args.dist = args.dist.expanduser().resolve()
    args.brush = args.brush.expanduser().resolve()
    with exclusive(args.work_dir):
        if args.watch:
            watch(args)
        elif args.local:
            process(args, args.run_id or args.local.name, args.local.expanduser().resolve())
        else:
            name, source, transport, pull_s, wait_s = wait_for_run(args)
            process(args, name, source, pull_s, transport, wait_s)


if __name__ == '__main__':
    main()
