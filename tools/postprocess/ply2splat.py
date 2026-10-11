"""Brush/3DGS .ply -> antimatter15 .splat (32 B/splat), biggest+most opaque first."""
import sys, numpy as np
src, dst = sys.argv[1], sys.argv[2]
raw = open(src, 'rb').read()
end = raw.index(b'end_header\n') + len(b'end_header\n')
props = [l.split()[-1].decode() for l in raw[:end].splitlines() if l.startswith(b'property')]
n = int([l for l in raw[:end].splitlines() if l.startswith(b'element vertex')][0].split()[-1])
a = np.frombuffer(raw[end:], dtype=np.float32, count=n * len(props)).reshape(n, len(props))
c = {p: a[:, i] for i, p in enumerate(props)}
pos = np.stack([c['x'], c['y'], c['z']], 1)
scale = np.exp(np.stack([c['scale_0'], c['scale_1'], c['scale_2']], 1))
op = 1 / (1 + np.exp(-c['opacity']))
rgb = np.clip(0.5 + 0.28209479 * np.stack([c['f_dc_0'], c['f_dc_1'], c['f_dc_2']], 1), 0, 1)
q = np.stack([c['rot_0'], c['rot_1'], c['rot_2'], c['rot_3']], 1); q /= np.linalg.norm(q, axis=1, keepdims=True)
order = np.argsort(-(scale.prod(1) * op))
rec = np.zeros(n, dtype=[('p', '<f4', 3), ('s', '<f4', 3), ('c', 'u1', 4), ('r', 'u1', 4)])
rec['p'], rec['s'] = pos[order], scale[order]
rec['c'][:, :3] = (rgb[order] * 255).astype(np.uint8); rec['c'][:, 3] = (op[order] * 255).astype(np.uint8)
rec['r'] = np.clip(q[order] * 128 + 128, 0, 255).astype(np.uint8)
rec.tofile(dst)
print(n, 'splats ->', dst, 'center', np.median(pos, 0).round(2), 'p5', np.percentile(pos, 5, 0).round(1), 'p95', np.percentile(pos, 95, 0).round(1))
