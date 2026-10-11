"""Build a Brush seed (init.ply, float xyz + uchar rgb) from the rover's dots and point transforms.json at it.

  python seed_from_dots.py DATASET_DIR points.bin [dense.xyzrgb]
points.bin: Jetson /map/points.bin (u32 count + 16 B records: f32 xyz, rgb, pad).
dense.xyzrgb: optional 16 B records (f32 xyz, rgb, pad), e.g. paged /map/dense.bin converted to metres.
Same ARKit session as the photos only. If the run saver already wrote init.ply, skip this and just
set "ply_file_path": "init.ply" in transforms.json.
"""
import json, sys
import numpy as np, open3d as o3d

ds, files = sys.argv[1], sys.argv[2:]
def load(path, off):
    a = np.frombuffer(open(path, 'rb').read()[off:], dtype=np.dtype([('p', '<f4', 3), ('c', 'u1', 4)]))
    return a['p'].astype(np.float64), a['c'][:, :3]
parts = [load(files[0], 4)] + [load(f, 0) for f in files[1:]]
pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.vstack([p for p, _ in parts])))
pc.colors = o3d.utility.Vector3dVector(np.vstack([c for _, c in parts]) / 255)
pc, _ = pc.remove_statistical_outlier(16, 2.0)
P = np.asarray(pc.points, np.float32); C = (np.asarray(pc.colors) * 255).round().astype(np.uint8)
rec = np.zeros(len(P), dtype=[('x', '<f4'), ('y', '<f4'), ('z', '<f4'), ('red', 'u1'), ('green', 'u1'), ('blue', 'u1')])
rec['x'], rec['y'], rec['z'] = P.T; rec['red'], rec['green'], rec['blue'] = C.T
hdr = (f"ply\nformat binary_little_endian 1.0\nelement vertex {len(P)}\nproperty float x\nproperty float y\n"
       "property float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n")
open(f'{ds}/init.ply', 'wb').write(hdr.encode() + rec.tobytes())
t = json.load(open(f'{ds}/transforms.json')); t['ply_file_path'] = 'init.ply'; json.dump(t, open(f'{ds}/transforms.json', 'w'))
print('seed points', len(P))
