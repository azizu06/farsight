"""List the continuous drives inside a capture and optionally cut one out.

  python find_drives.py RUN_DIR                    # print drives
  python find_drives.py RUN_DIR --cut A B OUT_DIR  # copy frames A..B (inclusive) into OUT_DIR
A drive is a stretch of motion (>1 cm or >2 deg per frame) with pauses under 12 s.
"""
import json, shutil, sys
from pathlib import Path
import numpy as np

run = Path(sys.argv[1]); t = json.load(open(run / 'transforms.json')); fr = t['frames']
M = [np.array(f['transform_matrix']) for f in fr]; P = np.array([m[:3, 3] for m in M])
T = np.array([f.get('t_capture', i / 2.5) for i, f in enumerate(fr)]); T -= T[0]
yaw = np.degrees(np.unwrap([np.arctan2(-m[0, 2], -m[2, 2]) for m in M]))
mov = np.r_[0, (np.linalg.norm(np.diff(P, axis=0), axis=1) > 0.01) | (np.abs(np.diff(yaw)) > 2)]
if '--cut' in sys.argv:
    i = sys.argv.index('--cut'); a, b, out = int(sys.argv[i + 1]), int(sys.argv[i + 2]), Path(sys.argv[i + 3])
    (out / 'images').mkdir(parents=True, exist_ok=True)
    seg = fr[a:b + 1]
    for f in seg: shutil.copy2(run / f['file_path'], out / f['file_path'])
    json.dump(dict(t, frames=seg), open(out / 'transforms.json', 'w'))
    print(f'cut {len(seg)} frames -> {out}'); sys.exit()
idx = np.where(mov)[0]
if not len(idx): print('no motion in this run'); sys.exit()
drives, s, p = [], idx[0], idx[0]
for i in idx[1:]:
    if T[i] - T[p] > 12: drives.append((s, p)); s = i
    p = i
drives.append((s, p))
for a, b in drives:
    path = np.linalg.norm(np.diff(P[a:b + 1], axis=0), axis=1).sum()
    print(f'frames {a:4d}-{b:4d} ({b - a + 1:3d})  {T[b] - T[a]:5.0f} s  path {path:5.2f} m  moving {mov[a:b + 1].sum():3d}')
