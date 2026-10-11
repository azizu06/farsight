"""Dots -> watertight-ish colored surface (Poisson), trimmed to where dots actually are."""
import struct, sys, time
import numpy as np, open3d as o3d

def load(path, off):
    b = open(path, 'rb').read()[off:]
    a = np.frombuffer(b, dtype=np.dtype([('p', '<f4', 3), ('c', 'u1', 4)]))
    return a['p'].astype(np.float64), a['c'][:, :3] / 255.0

pm, cm = load('dots/points.bin', 4)
pd, cd = load('dots/dense.xyzrgb', 0)
pcd = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.vstack([pm, pd])))
pcd.colors = o3d.utility.Vector3dVector(np.vstack([cm, cd]))
t = time.time()
pcd = pcd.voxel_down_sample(0.02)
pcd, _ = pcd.remove_statistical_outlier(nb_neighbors=16, std_ratio=2.0)
pcd.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=0.15, max_nn=30))
pcd.orient_normals_consistent_tangent_plane(15)
mesh, dens = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=int(sys.argv[1]) if len(sys.argv) > 1 else 9)
dens = np.asarray(dens)
mesh.remove_vertices_by_mask(dens < np.quantile(dens, 0.12))
# keep only surface near real dots (Poisson balloons into empty space otherwise)
tree = o3d.geometry.KDTreeFlann(pcd)
v = np.asarray(mesh.vertices)
far = np.array([tree.search_knn_vector_3d(x, 1)[2][0] > 0.12**2 for x in v])
mesh.remove_vertices_by_mask(far)
mesh.remove_degenerate_triangles(); mesh.remove_unreferenced_vertices()
mesh = mesh.simplify_quadric_decimation(120000)
mesh.compute_vertex_normals()
print('points', len(pcd.points), 'verts', len(mesh.vertices), 'tris', len(mesh.triangles), f'{time.time()-t:.1f}s')
o3d.io.write_triangle_mesh('dots/room_surface.ply', mesh)
V = np.asarray(mesh.vertices, np.float32); C = (np.clip(np.asarray(mesh.vertex_colors), 0, 1) * 255).astype(np.uint8)
I = np.asarray(mesh.triangles, np.uint32).ravel()
rec = np.zeros(len(V), dtype=np.dtype([('p', '<f4', 3), ('c', 'u1', 4)])); rec['p'] = V; rec['c'][:, :3] = C
open('dots-page/surf_poisson.bin', 'wb').write(struct.pack('<II', len(V), len(I)) + rec.tobytes() + I.astype('<u4').tobytes())
