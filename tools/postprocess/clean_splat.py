"""Clean a .splat: drop blobs far from measured LiDAR dots or the rover path, giant/needle blobs, and faint ones."""
import sys, numpy as np, open3d as o3d, json
src, dst, seed, tf = sys.argv[1:5]
rec = np.fromfile(src, dtype=[('p','<f4',3),('s','<f4',3),('c','u1',4),('r','u1',4)])
P, S, A = rec['p'].astype(np.float64), rec['s'], rec['c'][:, 3] / 255
dots = o3d.io.read_point_cloud(seed); tree = o3d.geometry.KDTreeFlann(dots)
cams = np.array([np.array(f['transform_matrix'])[:3, 3] for f in json.load(open(tf))['frames']])
near_dot = np.array([tree.search_knn_vector_3d(p, 1)[2][0] for p in P]) < 0.20 ** 2
near_cam = np.min(np.linalg.norm(P[:, None, :] - cams[None, ::4, :], axis=2), axis=1) < 6.0
smax, smin = S.max(1), S.min(1)
ok = near_cam & (near_dot | (A > 0.6)) & (smax < 0.35) & (smax / np.maximum(smin, 1e-4) < 25) & (A > 0.08)
print(f"kept {ok.sum()} of {len(rec)}; far-from-dots {(~near_dot).sum()}, far-from-path {(~near_cam).sum()}, giant {(smax>=0.35).sum()}, needles {(smax/np.maximum(smin,1e-4)>=25).sum()}, faint {(A<=0.08).sum()}")
rec[ok].tofile(dst)
