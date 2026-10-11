"""Drop frames that hurt a splat: tracking not normal, blurry (Laplacian var, bottom 15%), or near-duplicates while parked."""
import json, shutil, sys, numpy as np, cv2
from pathlib import Path
src, dst = Path(sys.argv[1]), Path(sys.argv[2])
t = json.load(open(src / 'transforms.json')); fr = t['frames']
def sharp(p): g = cv2.imread(str(src / p), cv2.IMREAD_GRAYSCALE); return cv2.Laplacian(g, cv2.CV_64F).var()
s = np.array([sharp(f['file_path']) for f in fr]); cut = np.percentile(s, 15)
# Group parked stretches (pose within 5 mm / 0.5 deg of the group's first frame), keep the sharpest of each group.
why = {'tracking': 0, 'blur': 0, 'still': 0}
groups, start = [], None
for f, sv in zip(fr, s):
    if f.get('tracking', 'normal') != 'normal': why['tracking'] += 1; continue
    m = np.array(f['transform_matrix'])
    if start is not None:
        d = np.linalg.norm(m[:3, 3] - start[:3, 3]); ang = np.degrees(np.arccos(np.clip((np.trace(start[:3, :3].T @ m[:3, :3]) - 1) / 2, -1, 1)))
        if d < 0.005 and ang < 0.5: groups[-1].append((sv, f)); continue
    groups.append([(sv, f)]); start = m
best = [max(g, key=lambda x: x[0]) for g in groups]
why['still'] = sum(len(g) - 1 for g in groups)
cut = np.percentile([b[0] for b in best], 10)
keep = [f for sv, f in best if sv >= cut]; why['blur'] = len(best) - len(keep)
(dst / 'images').mkdir(parents=True, exist_ok=True)
for f in keep: shutil.copy2(src / f['file_path'], dst / f['file_path'])
t['frames'] = keep
json.dump(t, open(dst / 'transforms.json', 'w'))
print(f'kept {len(keep)} of {len(fr)}; dropped {why}; blur cutoff {cut:.0f}')
