"""Pinned CPU witness for PR162. No ROS, device, GPU, server or motor access.

Run from the repository root with the project's NumPy environment:
  .venv/bin/python docs/qualification/nav-open-space-review-20261010/witness.py --repo .

Without --candidate-file, reads the pinned PR file from local Git, falling back
to its immutable public GitHub URL. Verifies its exact Git blob before executing.
Production codec functions are extracted unchanged from the pinned baseline;
the synthetic support map is free and FORMALLY FRESH (100ms age), but its physical
free-floor evidence is deliberately superseded by the current depth fixture.
"""
import argparse
import ast
import hashlib
import json
import math
from pathlib import Path
import struct
import subprocess
import sys
import types
import urllib.request

import numpy as np

BASE = '1af477195155f3343d8d9a0d0c878d343bf275dd'
PR = '04602b0fa811a1274567a91c6f15d5e29072da35'
PR_BASE = 'fa1a7a04ad78ae5fcd68093f1e5bd825f0a2db50'
PR_BLOB = '42ca1a914979ac5ae930db543aa287fa8ad68a55'
PATH = 'ros2/farsight_nav/farsight_nav/local_motion.py'


def source(repo, revision, path):
    return subprocess.check_output(['git', '-C', str(repo), 'show', revision + ':' + path])


def policy(name, local, frame_source):
    package = types.ModuleType(name); package.__path__ = []; sys.modules[name] = package
    frame = types.ModuleType(name + '.frames'); frame.__package__ = name
    exec(compile(frame_source, name + '/frames.py', 'exec'), frame.__dict__)
    sys.modules[frame.__name__] = frame; package.frames = frame
    module = types.ModuleType(name + '.local_motion'); module.__package__ = name
    exec(compile(local, name + '/local_motion.py', 'exec'), module.__dict__)
    return module.LocalMotion


def production_function(repo, path, name, namespace):
    tree = ast.parse(source(repo, BASE, path))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]), path + '@' + BASE, 'exec'), namespace)
    return namespace[name]


def scene(height=.15, drop=None, wall=6):
    v, u = np.mgrid[0:80, 0:120]
    ray = np.stack(((u - 60) / 60, -(v - 40) / 60, -np.ones_like(u, float)), axis=-1)
    floor = np.where(ray[..., 1] < 0, -height / np.minimum(ray[..., 1], -1e-9), np.inf)
    if drop is not None:
        floor = np.where(-ray[..., 2] * floor <= drop, floor, np.inf)
    wall_depth = np.where(height + ray[..., 1] * wall >= 0, wall, np.inf)
    depth = np.minimum(floor, wall_depth)
    depth = np.where(depth * np.linalg.norm(ray, axis=-1) <= 5, depth, 0)
    transform = np.eye(4); transform[1, 3] = height
    return dict(active=True, nav_command=None, nav_at=-100, robot=(0, 0, 0), depth=depth,
        header=dict(transform=transform.flatten(order='F').tolist(), floor_y=0, fx=60, fy=60, cx=60, cy=40),
        support=np.full((80, 100), 10.0), resolution=.1, origin=(-2, -4))


def command(cls, args):
    return cls().command(now=10, pose_at=10, depth_at=10, map_at=9.9, **args)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--candidate-file', type=Path); parser.add_argument('--out', type=Path)
    options = parser.parse_args()
    baseline = source(options.repo, BASE, PATH)
    if options.candidate_file:
        candidate = options.candidate_file.read_bytes()
    else:
        try:
            candidate = source(options.repo, PR, PATH)
        except subprocess.CalledProcessError:
            with urllib.request.urlopen('https://raw.githubusercontent.com/azizu06/farsight/' + PR + '/' + PATH, timeout=10) as response:
                candidate = response.read()
    blob = hashlib.sha1(b'blob ' + str(len(candidate)).encode() + b'\0' + candidate).hexdigest()
    assert blob == PR_BLOB, ('candidate does not match reviewed PR blob', blob)
    frames = source(options.repo, BASE, 'ros2/farsight_nav/farsight_nav/frames.py')
    before, proposed = policy('witness_baseline', baseline, frames), policy('witness_candidate', candidate, frames)
    encode = production_function(options.repo, 'jetson/nav.py', 'encode_depth', dict(np=np, math=math, json=json, struct=struct, PROTOCOL=1))
    decode = production_function(options.repo, 'ros2/farsight_nav/farsight_nav/protocol.py', 'decode_depth', dict(np=np, json=json, struct=struct))
    result = dict(scope='CPU fixtures only; no physical camera or driving qualification',
        production_baseline_sha=BASE, pr_head_sha=PR, pr_base_sha=PR_BASE, candidate_local_motion_blob=blob,
        freshness=dict(now=10, pose_at=10, depth_at=10, map_at=9.9, map_age_s=.1, map_expiry_s=2.5,
                       map_formally_fresh=True, map_content='previously observed free floor, superseded by current scene'))
    args = scene(); args['depth'][:30] = .2
    frame = types.SimpleNamespace(depth=args['depth'], confidence=np.ones_like(args['depth'], np.uint8),
        calibration=types.SimpleNamespace(fx=60, fy=60, cx=60, cy=40),
        transform=np.asarray(args['header']['transform']).reshape(4, 4, order='F'), header={'frame_id':1, 'tracking':'normal'})
    obstacle = {}
    for confidence in (1, 0):
        frame.confidence[:30] = confidence
        header, depth = decode(encode(frame, floor_y=0, source='CPU fixture', age_ms=0))
        trial = {**args, 'depth':depth, 'header':header}
        obstacle[str(confidence)] = dict(encoded_obstacle_m=float(depth[10, 60]), baseline=command(before, trial), proposed=command(proposed, trial))
    assert obstacle['1']['baseline'][0] < 0 and obstacle['1']['proposed'][0] < 0
    assert obstacle['0']['baseline'][0] == 0 and obstacle['0']['proposed'][0] > 0
    result['same_20cm_obstacle_low_confidence'] = obstacle
    args = scene(height=.08, drop=.6, wall=8)
    drop = dict(camera_height_m=.08, drop_ahead_m=.6,
        valid_depth_pixels=int(((args['depth'] > .15) & (args['depth'] <= 4)).sum()),
        baseline=command(before, args), proposed=command(proposed, args))
    assert drop['valid_depth_pixels'] >= 16 and drop['baseline'][0] == 0 and drop['proposed'][0] > 0
    result['floor_drop'] = drop
    invalid = {}
    for label, value in [('NaN', float('nan')), ('positive_inf', float('inf')), ('negative1', -1)]:
        depth = np.full((80, 120), value)
        invalid[label] = dict(baseline=before.depth_clear([(0.55, 0, .25)], depth, scene()['header']),
            proposed=proposed.depth_clear([(0.55, 0, .25)], depth, scene()['header']))
        assert not invalid[label]['baseline'] and invalid[label]['proposed']
    result['invalid_upper_sample'] = invalid
    print(json.dumps(result, indent=2))
    if options.out: options.out.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
