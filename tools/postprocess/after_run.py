#!/usr/bin/env python3
"""Mac post-run Brush pipeline. See AFTER_RUN.md for commands and measured budgets."""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import urlencode
from urllib.request import urlopen

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
    tf = run / "transforms.json"
    seed = run / "init.ply" if run.joinpath("init.ply").is_file() else run / "dense.ply"
    image_mtime = max((p.stat().st_mtime_ns for p in images), default=0)
    out[run.name] = [len(images), max([image_mtime] + [p.stat().st_mtime_ns for p in (tf, seed) if p.is_file()]),
                     seed.stat().st_size if seed.is_file() else 0,
                     tf.stat().st_size if tf.is_file() else 0, image_mtime]
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
            if ((p / 'init.ply').is_file() or (p / 'dense.ply').is_file()) and
            ((p / 'transforms.json').is_file() or (p / 'photos/transforms.json').is_file())}


def saved_runs(args):
    return {**local_runs(args.local_root), **local_runs(args.work_dir / 'captures')}


def eval_images(directory):
    # Sai's fork retains images/ in eval paths; Brush v0.3.0 writes flat paths.
    return directory / 'images' if (directory / 'images').is_dir() else directory


def trainer_env(work):
    return dict(os.environ, CUBECL_CACHE_DIR=str(work / 'gpu-cache/cubecl'),
                XDG_CACHE_HOME=str(work / 'gpu-cache'))


def run_order(name):
    stamp = re.search(r'\d{8}T\d{6}', name)
    return stamp.group(0) if stamp else name


def finished(previous, current, now, stable_seconds=60, require_seed=True):
    """Track capture signatures. Any active capture defers ALL remote pulls."""
    observations = {}
    for name, signature in current.items():
        old = previous.get(name)
        since = old[1] if old and old[0] == signature else now
        observations[name] = (signature, since)
    ready = [name for name, (_, since) in observations.items() if now - since >= stable_seconds]
    idle = len(ready) == len(current)
    return observations, [name for name in ready if (current[name][2] or not require_seed) and current[name][3]] if idle else []


def recent_frames(transforms, limit=150, seconds=180, gap=60):
    """Keep the newest moving interval, with evenly spaced camera coverage."""
    import numpy as np
    frames = transforms['frames']
    if not limit or len(frames) < 2:
        return frames
    matrices = np.asarray([frame['transform_matrix'] for frame in frames])
    positions = matrices[:, :3, 3]
    yaw = np.degrees(np.unwrap(np.arctan2(-matrices[:, 0, 2], -matrices[:, 2, 2])))
    moving = np.flatnonzero((np.linalg.norm(np.diff(positions, axis=0), axis=1) > .01) |
                           (np.abs(np.diff(yaw)) > 2)) + 1
    if not len(moving):
        raise RuntimeError('No motion in this capture')
    times = [frame.get('t_capture', i / 2.5) for i, frame in enumerate(frames)]
    last = int(moving[-1])
    first = last
    for index in reversed(moving[:-1]):
        if times[first] - times[index] > gap or times[last] - times[index] > seconds:
            break
        first = int(index)
    first = max(0, first - 1)
    indices = np.linspace(first, last, min(limit, last - first + 1), dtype=int)
    return [frames[int(index)] for index in indices]


def retained_seed(args, source, transforms):
    """Use the local relay's retained measured RGB-D for this exact ARKit session."""
    import numpy as np
    params = {'after': 0, 'snapshot': 'true'}
    parts = []
    pages = 0
    while True:
        with urlopen(args.base_url.rstrip('/') + '/dense/delta.bin?' + urlencode(params), timeout=5) as response:
            body = response.read()
        length = struct.unpack_from('<I', body)[0]
        meta = json.loads(body[4:4 + length])
        if meta['source'] != [transforms['session_id'], transforms['map_epoch']]:
            raise RuntimeError('Retained measured cloud belongs to another capture session')
        parts.append(body[4 + length:])
        pages += 1
        if not meta.get('more'):
            break
        params.update(epoch=meta['epoch'], after=meta['cursor'], anchor=meta['anchor'])
    points = np.frombuffer(b''.join(parts), dtype=[('x', '<i4'), ('y', '<i4'), ('z', '<i4'),
                          ('r', 'u1'), ('g', 'u1'), ('b', 'u1'), ('op', 'u1')])
    points = points[points['op'] == 1]
    if not len(points):
        raise RuntimeError('No retained measured RGB-D points')
    output = np.empty(len(points), dtype=[('x', '<f4'), ('y', '<f4'), ('z', '<f4'),
                                         ('red', 'u1'), ('green', 'u1'), ('blue', 'u1')])
    for axis in ('x', 'y', 'z'):
        output[axis] = points[axis] / 1000
    for target, original in [('red', 'r'), ('green', 'g'), ('blue', 'b')]:
        output[target] = points[original]
    header = ('ply\nformat binary_little_endian 1.0\ncomment retained same-session measured RGB-D\n'
              f'element vertex {len(output)}\nproperty float x\nproperty float y\nproperty float z\n'
              'property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n')
    temporary = source / 'init.ply.tmp'
    temporary.write_bytes(header.encode() + output.tobytes())
    temporary.replace(source / 'init.ply')
    (source / 'seed-source.json').write_text(json.dumps(dict(
        source='local relay retained same-session measured RGB-D', points=len(output), pages=pages)))


def open_walk(receipt):
    """The judging completion notifier: open the published face-enabled room in Chrome."""
    opened = command(['open', '-a', 'Google Chrome', receipt['walk_url']], capture_output=True, text=True)
    return {'chrome_open_returncode': opened.returncode,
            'chrome_open_utc': datetime.now(timezone.utc).isoformat()}


def wait_for_run(args):
    observations = {}
    started = time.monotonic()
    while True:
        try:
            current = inventory(args.host)
        except subprocess.CalledProcessError:
            runs = saved_runs(args)
            name = args.run_id or (max(runs, key=run_order) if runs else None)
            if name in runs:
                print('Jetson unreachable; using saved LOCAL run, no pull.', flush=True)
                return name, runs[name], 'offline-local', 0, time.monotonic() - started
            raise RuntimeError('Jetson unreachable and no complete local run is available')
        observations, ready = finished(observations, current, time.monotonic(), args.idle_seconds,
                                       require_seed=not args.demo)
        name = args.run_id or (max(current, key=run_order) if args.demo and current else max(ready, key=run_order) if ready else None)
        if name in ready:
            stage = time.monotonic()
            source = pull(args, name)
            return name, source, 'rsync', time.monotonic() - stage, stage - started
        print(f'Waiting for all capture image counts/mtimes to stay unchanged for >={args.idle_seconds} s.', flush=True)
        time.sleep(args.poll_seconds)


def pull(args, name):
    destination = args.work_dir / 'captures' / name
    destination.mkdir(parents=True, exist_ok=True)
    if getattr(args, 'recent_frames', 0):
        bandwidth = [] if args.demo else ['--bwlimit=' + str(args.bwlimit)]
        # Metadata first, only after capture-idle observation; never pull the full photo archive.
        command(['rsync', '-a', *bandwidth, '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=5',
                 '--include=/transforms.json', '--include=/init.ply', '--include=/dense.ply',
                 '--exclude=*', f'{args.host}:captures/{name}/', str(destination) + '/'])
        transforms = json.loads((destination / 'transforms.json').read_text())
        selected = recent_frames(transforms, args.recent_frames, args.recent_seconds, args.drive_gap_seconds)
        listing = destination / 'selected-photos.txt'
        listing.write_text('\n'.join(frame['file_path'] for frame in selected) + '\n')
        command(['rsync', '-a', *bandwidth, '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=5',
                 '--files-from=' + str(listing), f'{args.host}:captures/{name}/', str(destination) + '/'])
        transforms['frames'] = selected
        (destination / 'transforms.json').write_text(json.dumps(transforms))
        if not (destination / 'init.ply').is_file() and not (destination / 'dense.ply').is_file():
            retained_seed(args, destination, transforms)
        return destination
    command(['rsync', '-a', *([] if args.demo else ['--bwlimit=' + str(args.bwlimit)]),
             '-e', 'ssh -o BatchMode=yes -o ConnectTimeout=5',
             '--include=/images/', '--include=/images/*.jpg', '--include=/transforms.json',
             '--include=/init.ply', '--include=/dense.ply', '--exclude=*',
             f'{args.host}:captures/{name}/', str(destination) + '/'])
    return destination


def prepare(source, destination, drive_gap_seconds=60, frame_limit=0, recent_seconds=180):
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = False
    dataset = source if (source / 'transforms.json').is_file() else source / 'photos'
    transforms = json.loads((dataset / 'transforms.json').read_text())
    original_frames = len(transforms['frames'])
    if frame_limit:
        transforms['frames'] = recent_frames(transforms, frame_limit, recent_seconds, drive_gap_seconds)
    clean = destination / 'decoded'
    clean.mkdir(parents=True)
    frames, rejected, normalized = [], [], []
    for frame in transforms['frames']:
        image = dataset / frame['file_path']
        try:
            with Image.open(image) as decoded:
                decoded.verify()
            with Image.open(image) as decoded:
                decoded.load()
                pixels = decoded.copy() if image.stat().st_size < 16387 else None
        except (OSError, ValueError, SyntaxError) as error:
            rejected.append({'file': frame['file_path'], 'reason': str(error)})
            continue
        target = clean / frame['file_path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(image, target)
        if pixels is not None:
            # Preserve valid small photos; Brush requires a 16,387-byte initial read.
            png = target.with_suffix('.png')
            pixels.save(png)
            with png.open('ab') as padded:
                padded.write(b'\0' * max(0, 20000 - png.stat().st_size))
            normalized.append(frame['file_path'])
            frame = dict(frame, file_path=str(png.relative_to(clean)))
        frames.append(frame)
    if not frames:
        raise RuntimeError('No decodable images')
    transforms['frames'] = frames
    (clean / 'transforms.json').write_text(json.dumps(transforms))
    drives = command([sys.executable, ROOT / 'tools/postprocess/find_drives.py', clean,
                      '--gap-seconds', drive_gap_seconds],
                     capture_output=True, text=True).stdout
    print(drives, flush=True)
    segments = [(int(a), int(b)) for a, b in re.findall(r'frames\s+(\d+)-\s*(\d+)', drives)]
    if not segments:
        raise RuntimeError('No continuous drive found')
    def duration(segment):
        a, b = segment
        return frames[b].get('t_capture', b / 2.5) - frames[a].get('t_capture', a / 2.5)
    a, b = (0, len(frames) - 1) if frame_limit or len(segments) == 1 else max(segments, key=duration)
    cut = destination / 'drive'
    command([sys.executable, ROOT / 'tools/postprocess/find_drives.py', clean, '--cut', a, b, cut])
    seed_source = source / 'init.ply' if (source / 'init.ply').is_file() else source / 'dense.ply'
    shutil.copy2(seed_source, cut / 'init.ply')
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
                 'normalized_short_jpegs': normalized, 'drive_gap_seconds': drive_gap_seconds,
                 'selection': 'recent-motion' if frame_limit else 'whole-run' if len(segments) == 1 else 'longest-drive',
                 'archive_frames': original_frames, 'frame_limit': frame_limit,
                 'seed_file': str(seed_source),
                 'drive_frames': b - a + 1, 'drive_duration_s': duration((a, b)), 'seed_points': points}


def publish(splat, dist, name):
    scenes = dist / 'scenes'
    scenes.mkdir(parents=True, exist_ok=True)
    return atomic_copy(splat, scenes / (name + '.ply'))


def atomic_copy(source, target):
    temporary = target.with_name(target.name + '.tmp')
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)
    return target


class FaceJob:
    """CPU face extraction overlaps Brush; always reap it when a run is stopped."""
    def __init__(self, args, dataset, output):
        self.output = output / 'faces'
        self.output.mkdir()
        self.log = (output / 'faces.log').open('w')
        models = args.work_dir / 'face-models'
        models.mkdir(exist_ok=True)
        env = dict(os.environ, FACE_MODEL_PATH=str(models / 'yolo11n-pose.pt'),
                   YOLO_CONFIG_DIR=str(args.work_dir / 'faces-config'),
                   MPLCONFIGDIR=str(args.work_dir / 'faces-mpl'),
                   TORCH_HOME=str(args.work_dir / 'faces-torch'),
                   OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2', MKL_NUM_THREADS='2')
        self.started = time.monotonic()
        try:
            self.child = subprocess.Popen(['nice', '-n', '10', str(args.faces_python),
                str(ROOT / 'tools/postprocess/faces/face_pins.py'), str(dataset),
                str(dataset / 'init.ply'), str(self.output)], cwd=models, env=env,
                stdout=self.log, stderr=subprocess.STDOUT)
        except Exception:
            self.log.close()
            raise
        self.thread = threading.Thread(target=self._wait, daemon=True)
        self.thread.start()

    def _wait(self):
        self.exit_code = self.child.wait()
        self.seconds = time.monotonic() - self.started

    def finish(self):
        self.thread.join()
        return {'faces_s': self.seconds, 'faces_exit_code': self.exit_code}

    def close(self):
        if self.child.poll() is None:
            self.child.terminate()
        self.thread.join()
        self.log.close()


def publish_faces(source, dist, name):
    target = dist / 'scenes' / (name + '-faces')
    (target / 'faceframes').mkdir(parents=True, exist_ok=True)
    for photo in (source / 'faceframes').glob('*.jpg'):
        atomic_copy(photo, target / 'faceframes' / photo.name)
    # Publish the manifest last, once all photos it references are available.
    atomic_copy(source / 'faces.json', target / 'faces.json')
    return f'/scenes/{name}-faces/faces.json'


def start_pose(dataset, eye_height=1.6):
    import numpy as np
    frames = json.loads((dataset / 'transforms.json').read_text())['frames']
    normal = [frame for frame in frames if frame.get('tracking', 'normal') == 'normal'] or frames
    frame = normal[len(normal) // 2]
    matrix = np.array(frame['transform_matrix'])
    # flip=1 rotates the PLY 180 degrees about X; transform the measured camera too.
    flip = np.array([1, -1, -1])
    camera = matrix[:3, 3] * flip
    forward = -matrix[:3, 2] * flip
    floor = camera - np.array([0, eye_height, 0])
    yaw = math.degrees(math.atan2(-forward[0], -forward[2]))
    return {'frame_id': frame.get('frame_id'), 'image': frame['file_path'],
            'camera_position': camera.tolist(), 'floor_position': floor.tolist(),
            'eye_height_m': eye_height, 'yaw_degrees': yaw, 'flip': 1}


@contextlib.contextmanager
def exclusive(work, name="pipeline.lock", blocking=False):
    work.mkdir(parents=True, exist_ok=True)
    with (work / name).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise RuntimeError('Another after_run/watch process owns this work directory')
        yield


def process(args, name, source, pull_s=0, transport='local', wait_s=0, last_image_mtime_ns=None):
    started = time.monotonic()
    output = args.work_dir / 'runs' / name / time.strftime('%Y%m%dT%H%M%S')
    output.mkdir(parents=True)
    receipt = {'run_id': name, 'source': str(source), 'transport': transport, 'pull_s': pull_s,
               'steps': args.steps, 'wait_for_end_s': wait_s, 'status': 'running',
               'quest_prune_skipped': args.skip_quest, 'prune_s': 0.0}
    def utc(seconds=None):
        return datetime.fromtimestamp(time.time() if seconds is None else seconds, timezone.utc).isoformat()
    receipt['pull_started_utc'] = utc(time.time() - pull_s)
    receipt['pull_done_utc'] = utc()
    if last_image_mtime_ns:
        receipt['last_photo_utc'] = utc(last_image_mtime_ns / 1e9)
    try:
        stage = time.monotonic()
        dataset, prep = prepare(source, output, args.drive_gap_seconds,
                                getattr(args, 'recent_frames', 0), getattr(args, 'recent_seconds', 180))
        receipt.update(prep, prep_s=time.monotonic() - stage)
        if (source / 'seed-source.json').is_file():
            receipt['retained_seed'] = json.loads((source / 'seed-source.json').read_text())
        exports = output / 'exports'
        exports.mkdir()
        work = args.work_dir / 'brush'
        work.mkdir(exist_ok=True)
        # Stock Brush's max-splats only limits later growth; cap its seed separately.
        subsample = max(1, math.ceil(prep['seed_points'] / args.seed_max_points)) if args.seed_max_points else 1
        steps_flag = '--total-train-iters' if args.trainer == 'turbo' else '--total-steps'
        argv = ['nice', '-n', '10', args.brush, dataset, steps_flag, args.steps,
                '--export-every', args.steps, '--export-path', exports,
                '--max-resolution', args.max_resolution, '--sh-degree', args.train_sh_degree,
                '--max-splats', args.max_splats, '--subsample-points', subsample,
                '--eval-split-every', '8', '--eval-every', args.steps, '--eval-save-to-disk', *args.brush_extra]
        if args.load_test:
            load_test = output / 'load-test'
            load_test.mkdir()
            stage = time.monotonic()
            with (output / 'load-test.log').open('w') as log:
                command(['nice', '-n', '10', args.brush, dataset, steps_flag, '1',
                         '--export-every', '1', '--export-path', load_test,
                         '--max-resolution', args.max_resolution, '--sh-degree', args.train_sh_degree,
                         '--subsample-points', subsample], cwd=work, env=trainer_env(args.work_dir),
                        stdout=log, stderr=subprocess.STDOUT)
            receipt['load_test_s'] = time.monotonic() - stage
        receipt['brush_command'] = [str(a) for a in argv]
        faces = None
        if getattr(args, 'faces', False):
            try:
                faces = FaceJob(args, dataset, output)
            except Exception as error:
                receipt['faces_error'] = str(error)
                print(f'Face extraction could not start: {error}', file=sys.stderr, flush=True)
        try:
            stage = time.monotonic()
            with (output / 'brush.log').open('w') as log:
                command(argv, cwd=work, env=trainer_env(args.work_dir), stdout=log, stderr=subprocess.STDOUT)
            receipt['train_s'] = time.monotonic() - stage
            receipt['train_done_utc'] = utc()
            stage = time.monotonic()
            if faces:
                receipt.update(faces.finish())
            receipt['faces_wait_after_train_s'] = time.monotonic() - stage
        finally:
            if faces:
                faces.close()
        receipt['steps_per_second_wall'] = args.steps / receipt['train_s']
        receipt['suggested_120s_steps'] = max(1, round(args.steps * 120 / receipt['train_s']))
        raw = sorted(exports.glob('*.ply'))[-1]
        # The Mac demo becomes ready before optional Quest pruning and scoring.
        stage = time.monotonic()
        desktop_name = args.desktop_name or name + '-desktop'
        receipt['desktop_published'] = str(publish(raw, args.dist, desktop_name))
        atomic_copy(dataset / 'transforms.json', args.dist / 'scenes' / (name + '-transforms.json'))
        atomic_copy(ROOT / 'tools/postprocess/walk.html', args.dist / 'scenes/walk.html')
        vendor = args.dist / 'scenes/vendor'
        vendor.mkdir(exist_ok=True)
        for asset in (ROOT / 'tools/postprocess/vendor').iterdir():
            atomic_copy(asset, vendor / asset.name)
        face_query = {}
        if faces and receipt['faces_exit_code'] == 0:
            face_query['faces'] = publish_faces(faces.output, args.dist, name)
            receipt['faces_count'] = len(json.loads((faces.output / 'faces.json').read_text())['faces'])
        elif faces:
            receipt['faces_error'] = f"Face extraction exited {receipt['faces_exit_code']}; see faces.log"
        receipt['desktop_publish_s'] = time.monotonic() - stage
        receipt['start_pose'] = pose = start_pose(dataset, args.eye_height)
        viewer = {'type': 'ply', 'flip': '1', 'transforms': f'scenes/{name}-transforms.json',
                  'pos': ','.join(f'{v:.5f}' for v in pose['floor_position']),
                  'yaw': f"{pose['yaw_degrees']:.3f}"}
        receipt['desktop_url'] = args.base_url.rstrip('/') + '/splat.html?' + urlencode({
            **viewer, 'scene': f'scenes/{desktop_name}.ply', 'sh': args.train_sh_degree})
        receipt['walk_url'] = args.base_url.rstrip('/') + '/scenes/walk.html?' + urlencode({
            'scene': f'/scenes/{desktop_name}.ply', 'transforms': f'/scenes/{name}-transforms.json',
            'sh': min(args.train_sh_degree, 2), 'start': 'mid',
            'info': f'{name}: {args.steps} steps, full model', **face_query})
        receipt['desktop_ready_s'] = time.monotonic() - started + pull_s
        receipt['url_ready_utc'] = utc()
        if last_image_mtime_ns:
            receipt['last_photo_to_ready_s'] = time.time() - last_image_mtime_ns / 1e9
        receipt['after_observation_start_ready_s'] = receipt['desktop_ready_s'] + wait_s
        print('DESKTOP READY: ' + receipt['walk_url'], flush=True)
        (output / 'timing-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        stage = time.monotonic()
        if not args.skip_quest:
            pruned = command([sys.executable, '-m', 'tools.splat.prune', raw, output / 'splat.ply',
                              '--max-splats', '300000', '--sh-degree', '1'], cwd=ROOT,
                             capture_output=True, text=True)
            receipt.update(prune_s=time.monotonic() - stage, prune=json.loads(pruned.stdout))
            stage = time.monotonic()
            receipt['published'] = str(publish(output / 'splat.ply', args.dist, name))
            receipt['publish_s'] = time.monotonic() - stage
            receipt['url'] = args.base_url.rstrip('/') + '/splat.html?' + urlencode({
                **viewer, 'scene': f'scenes/{name}.ply', 'sh': '1'})
        stage = time.monotonic()
        evaluations = list(exports.glob('eval_*'))
        if evaluations:
            score = command([sys.executable, ROOT / 'tools/postprocess/score_heldout.py',
                             output / 'decoded/images', eval_images(evaluations[-1])], capture_output=True, text=True)
            receipt['heldout'] = score.stdout.strip()
        receipt['score_s'] = time.monotonic() - stage
        receipt['status'] = 'complete'
        if getattr(args, 'open_chrome', False):
            # One final open, once every selected model/sidecar and optional stage is complete.
            try:
                receipt.update(open_walk(receipt))
            except Exception as error:
                receipt['chrome_open_error'] = str(error)
                print(f'Chrome open failed: {error}', file=sys.stderr, flush=True)
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
    print(f'Watcher armed: host={args.host} since={args.since or "all"} poll={args.poll_seconds}s', flush=True)
    observations = {}
    while True:
        tick = time.monotonic()
        try:
            current = inventory(args.host)
        except subprocess.CalledProcessError as error:
            # A powered-off Jetson cannot be pulled; complete saved datasets remain usable.
            print(f'SSH inventory unavailable for {args.host}; saved local runs only: '
                  f'{(error.stderr or str(error)).strip()}', file=sys.stderr, flush=True)
            candidates = [(name, path, 'offline-local') for name, path in saved_runs(args).items()]
        else:
            observations, ready = finished(observations, current, tick, args.idle_seconds,
                                           require_seed=not args.demo)
            newest = max(current, key=run_order) if current else None
            print(f'{time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())} '
                  f'inventory={len(current)} idle_ready={len(ready)} '
                  f'newest={newest} images={current[newest][0] if newest else 0}', flush=True)
            candidates = [(name, None, 'rsync') for name in ready]
            if args.demo:
                candidates = [candidate for candidate in candidates if candidate[0] == newest]
        if args.demo and candidates:
            candidates = [max(candidates, key=lambda candidate: run_order(candidate[0]))]
        for name, source, transport in sorted(candidates, key=lambda candidate: run_order(candidate[0]), reverse=True):
            stamp = re.search(r'\d{8}T\d{6}', name)
            if args.since and (not stamp or stamp.group(0) < args.since):
                continue
            if (args.work_dir / (name + '.done.json')).exists():
                continue
            # Process one run per tick; the next SSH observation must confirm the link is idle again.
            try:
                with exclusive(args.work_dir):
                    stage = time.monotonic()
                    source = source or pull(args, name)
                    pull_s = time.monotonic() - stage if transport == 'rsync' else 0
                    wait_s = tick - observations[name][1] if transport == 'rsync' else 0
                    process(args, name, source, pull_s, transport, wait_s,
                            current[name][4] if transport == 'rsync' and len(current[name]) > 4 else None)
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
    parser.add_argument('--demo', action='store_true', help='USB, newest only, 10s idle, unlimited rsync, 5k/300k seed')
    parser.add_argument('--local', type=Path, help='saved completed run; skips SSH and pull')
    parser.add_argument('--local-root', type=Path, default=Path.home() / 'farsight-captures')
    parser.add_argument('--host')
    parser.add_argument('--work-dir', type=Path, default=ROOT / '.cache/autosplat')
    parser.add_argument('--dist', type=Path, default=DIST)
    parser.add_argument('--base-url', default='http://localhost:8080')
    parser.add_argument('--steps', type=int)
    parser.add_argument('--fast', dest='steps', action='store_const', const=2000, default=argparse.SUPPRESS, help='Dechante fast option: 2000 steps')
    parser.add_argument('--max-resolution', type=int, default=1600)
    parser.add_argument('--max-splats', type=int, default=300000, help='training growth cap; turbo also caps its initial seed')
    parser.add_argument('--train-sh-degree', type=int, choices=range(4), default=3)
    parser.add_argument('--load-test', action='store_true', help='optional one-step diagnostic before training')
    parser.add_argument('--skip-quest', action='store_true', help='publish only the full-quality Mac scene')
    parser.add_argument('--faces', action='store_true', help='CPU face pins alongside Brush; enabled in demo mode')
    parser.add_argument('--open-chrome', action='store_true', help='open the walk URL in Chrome immediately after publication')
    parser.add_argument('--recent-frames', type=int, help='cap most recent moving photos; demo default 150, normal default 0/full')
    parser.add_argument('--recent-seconds', type=float, default=180, help='recent motion window before sampling photos')
    parser.add_argument('--faces-python', type=Path, help='Python with ultralytics/open3d; defaults to the faces venv')
    parser.add_argument('--desktop-name', help='override the full-quality scene filename stem')
    parser.add_argument('--wait-for-gpu', action='store_true', help='queue a manual trial behind an active automatic job')
    parser.add_argument('--seed-max-points', type=int, help="initial seed cap; 0 uses Dechante's full measured seed")
    parser.add_argument('--drive-gap-seconds', type=float, default=60, help='maximum parked pause within a drive')
    parser.add_argument('--eye-height', type=float, default=1.6, help='XR eye height above start floor, metres')
    parser.add_argument('--since', help='watch only run IDs at/after this UTC YYYYMMDDTHHMMSS timestamp')
    parser.add_argument('--trainer', choices=['stable', 'turbo'], default='stable')
    parser.add_argument('--brush', type=Path, help='trainer binary; BRUSH env overrides its default')
    parser.add_argument('--brush-extra', nargs=argparse.REMAINDER, default=[], help='extra trainer arguments, placed last')
    parser.add_argument('--poll-seconds', type=int, choices=range(10, 61), metavar='10..60')
    parser.add_argument('--bwlimit', type=int, default=2048, help='rsync KiB/s; default 2 MiB/s')
    args = parser.parse_args()
    args.host = args.host or ('jetson' if args.demo else 'jetson-ts')
    args.idle_seconds = 10 if args.demo else 60
    args.poll_seconds = args.poll_seconds or (10 if args.demo else 30)
    if not args.demo and args.poll_seconds < 30:
        parser.error('non-demo polling must be at least 30 seconds')
    args.steps = args.steps if args.steps is not None else int(os.environ.get('SPLAT_STEPS', '5000'))
    args.seed_max_points = args.seed_max_points if args.seed_max_points is not None else (300000 if args.demo else 0)
    args.faces = args.faces or args.demo
    args.recent_frames = args.recent_frames if args.recent_frames is not None else (150 if args.demo else 0)
    args.brush = args.brush or Path(os.environ.get('BRUSH', str(
        args.work_dir / 'turbo-target/release/brush-cli' if args.trainer == 'turbo' else BRUSH)))
    args.work_dir = args.work_dir.expanduser().resolve()
    # Keep the venv launcher path: resolving its symlink bypasses site-packages.
    args.faces_python = Path(os.path.abspath((args.faces_python or args.work_dir /
                                            'faces-venv/bin/python').expanduser()))
    args.dist = args.dist.expanduser().resolve()
    args.brush = args.brush.expanduser().resolve()
    if args.watch:
        with exclusive(args.work_dir, 'watcher.lock'):
            watch(args)
        return
    with exclusive(args.work_dir, blocking=args.wait_for_gpu):
        if args.local:
            process(args, args.run_id or args.local.name, args.local.expanduser().resolve())
        else:
            name, source, transport, pull_s, wait_s = wait_for_run(args)
            process(args, name, source, pull_s, transport, wait_s)


if __name__ == '__main__':
    main()
