use crate::core::{Mesh, PointCloud};

/// Binary PLY, float xyz + uchar rgb (same layout as /map/export.ply).
pub fn points(cloud: &PointCloud) -> Vec<u8> {
    let n = cloud.len();
    let mut out = format!("ply\nformat binary_little_endian 1.0\nelement vertex {n}\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n").into_bytes();
    out.reserve(n * 15);
    for i in 0..n {
        for v in cloud.pos[i] { out.extend_from_slice(&v.to_le_bytes()); }
        out.extend_from_slice(&cloud.col.get(i).copied().unwrap_or([200, 200, 200]));
    }
    out
}

/// Binary PLY mesh, float xyz + uchar rgb vertices, uchar/uint triangle lists.
pub fn mesh(mesh: &Mesh) -> Vec<u8> {
    let (n, f) = (mesh.pos.len(), mesh.triangles());
    let mut out = format!("ply\nformat binary_little_endian 1.0\nelement vertex {n}\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nelement face {f}\nproperty list uchar uint vertex_indices\nend_header\n").into_bytes();
    for i in 0..n {
        for v in mesh.pos[i] { out.extend_from_slice(&v.to_le_bytes()); }
        out.extend_from_slice(&mesh.col.get(i).copied().unwrap_or([200, 200, 200]));
    }
    for t in mesh.idx.chunks_exact(3) {
        out.push(3);
        for i in t { out.extend_from_slice(&i.to_le_bytes()); }
    }
    out
}
