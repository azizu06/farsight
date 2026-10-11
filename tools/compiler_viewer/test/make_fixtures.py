#!/usr/bin/env python3
"""Fixture generator for compiler_viewer dev until W4-W6 land real artefacts.

Writes into an existing compile output dir (e.g. from `bench --out`):
  mesh.ply   binary PLY mesh (room + furniture), per-vertex `support` metres
  splat.ply  INRIA-style 3DGS PLY (f_dc_*, opacity, scale_*, rot_*)

Usage: python3 make_fixtures.py OUT_DIR [--seed N]
"""
import math, random, struct, sys, os

def quad(a, b, c, d, tris):
    tris += [(a, b, c), (a, c, d)]

def add_box(verts, faces, cols, sup, cx, cy, cz, sx, sy, sz, color, support=0.0):
    i0 = len(verts)
    for dx, dy, dz in [(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),(-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)]:
        verts.append((cx + dx*sx/2, cy + dy*sy/2, cz + dz*sz/2))
        cols.append(color); sup.append(support)
    quad(i0+0,i0+1,i0+2,i0+3, faces); quad(i0+4,i0+6,i0+5,i0+7, faces)
    quad(i0+0,i0+4,i0+7,i0+3, faces); quad(i0+1,i0+2,i0+6,i0+5, faces)
    quad(i0+0,i0+1,i0+5,i0+4, faces); quad(i0+3,i0+7,i0+6,i0+2, faces)

def add_sphere(verts, faces, cols, sup, cx, cy, cz, r, color, lat=24, lon=36, support=0.0):
    i0 = len(verts)
    for j in range(lat + 1):
        th = math.pi * j / lat
        for i in range(lon + 1):
            ph = 2 * math.pi * i / lon
            verts.append((cx + r*math.sin(th)*math.cos(ph), cy + r*math.cos(th), cz + r*math.sin(th)*math.sin(ph)))
            cols.append(color); sup.append(support)
    for j in range(lat):
        for i in range(lon):
            a = i0 + j*(lon+1) + i; b = a + 1; c = a + lon + 1; d = c + 1
            if j: faces.append((a, c, b))
            if j < lat - 1: faces.append((b, c, d))

def subdiv_plane(verts, faces, cols, sup, x0, x1, z0, z1, y, nx, nz, color, bump=None, support_fn=None):
    i0 = len(verts)
    for j in range(nz + 1):
        for i in range(nx + 1):
            x = x0 + (x1 - x0) * i / nx; z = z0 + (z1 - z0) * j / nz
            yy = y + (bump(x, z) if bump else 0.0)
            verts.append((x, yy, z)); cols.append(color)
            sup.append(support_fn(x, z) if support_fn else 0.0)
    for j in range(nz):
        for i in range(nx):
            a = i0 + j*(nx+1) + i; b = a + 1; c = a + nx + 1; d = c + 1
            faces.append((a, c, b)); faces.append((b, c, d))

def write_ply(path, verts, cols, sup, faces, uvs=None):
    n, f = len(verts), len(faces)
    uv_line = "property float s\nproperty float t\n" if uvs else ""
    hdr = (f"ply\nformat binary_little_endian 1.0\nelement vertex {n}\n"
           "property float x\nproperty float y\nproperty float z\n"
           "property uchar red\nproperty uchar green\nproperty uchar blue\n"
           f"{uv_line}property float support\n"
           f"element face {f}\nproperty list uchar uint vertex_indices\nend_header\n").encode()
    with open(path, 'wb') as fh:
        fh.write(hdr)
        for i, v in enumerate(verts):
            fh.write(struct.pack('<fffBBB', *v, *cols[i]))
            if uvs: fh.write(struct.pack('<ff', *uvs[i]))
            fh.write(struct.pack('<f', sup[i]))
        for t in faces:
            fh.write(struct.pack('<BIII', 3, *t))

def write_splat(path, n, rng):
    # splats scattered over a fuzzy room-like shell + blob
    props = "property float x\nproperty float y\nproperty float z\n" \
            "property float f_dc_0\nproperty float f_dc_1\nproperty float f_dc_2\n" \
            "property float opacity\nproperty float scale_0\nproperty float scale_1\nproperty float scale_2\n" \
            "property float rot_0\nproperty float rot_1\nproperty float rot_2\nproperty float rot_3\n"
    hdr = f"ply\nformat binary_little_endian 1.0\nelement vertex {n}\n{props}end_header\n".encode()
    SH = 0.28209479177387814
    out = open(path, 'wb'); out.write(hdr)
    for _ in range(n):
        kind = rng.random()
        if kind < 0.45:   # floor-ish
            x, y, z = rng.uniform(-3, 3), rng.gauss(0.0, 0.03), rng.uniform(-2.5, 2.5)
            col = (0.55, 0.5, 0.42)
        elif kind < 0.7:  # walls-ish
            side = rng.choice([0, 1, 2, 3])
            x = rng.choice([-3, 3]) if side < 2 else rng.uniform(-3, 3)
            z = rng.choice([-2.5, 2.5]) if side >= 2 else rng.uniform(-2.5, 2.5)
            y = rng.uniform(0, 2.8); col = (0.62, 0.66, 0.7)
        elif kind < 0.85: # blob on "table"
            u, v = rng.random()*2*math.pi, rng.uniform(-1, 1)
            rr = 0.5
            x = 0.8 + rr*math.sqrt(1-v*v)*math.cos(u); y = 1.1 + rr*v; z = 0.3 + rr*math.sqrt(1-v*v)*math.sin(u)
            col = (0.8, 0.35, 0.2)
        else:             # scattered furniture splats
            x, y, z = rng.uniform(-2.5, 2.5), rng.uniform(0, 2.5), rng.uniform(-2, 2)
            col = (0.4 + rng.random()*0.3, 0.3 + rng.random()*0.2, 0.25)
        fdc = [(c - 0.5) / SH for c in col]
        s = [math.log(rng.uniform(0.004, 0.03)) for _ in range(3)]
        # random unit quaternion
        u1, u2, u3 = rng.random(), rng.random(), rng.random()
        q = (math.sqrt(1-u1)*math.sin(2*math.pi*u2), math.sqrt(1-u1)*math.cos(2*math.pi*u2),
             math.sqrt(u1)*math.sin(2*math.pi*u3), math.sqrt(u1)*math.cos(2*math.pi*u3))
        op = rng.gauss(2.2, 0.8)
        out.write(struct.pack('<fff', x, y, z))
        out.write(struct.pack('<fff', *fdc))
        out.write(struct.pack('<f', op))
        out.write(struct.pack('<fff', *s))
        out.write(struct.pack('<ffff', *q))
    out.close()

def main():
    out = sys.argv[1]
    seed = 7
    if '--seed' in sys.argv: seed = int(sys.argv[sys.argv.index('--seed') + 1])
    rng = random.Random(seed)

    verts, faces, cols, sup, uvs = [], [], [], [], None

    def hole_support(x, z):
        d = math.hypot(x - 1.0, z - 0.5)
        return max(0.0, min(0.12, (0.45 - d) * 0.28))

    # floor with an "interpolated fill" patch at support>0
    subdiv_plane(verts, faces, cols, sup, -3, 3, -2.5, 2.5, 0.0, 90, 75,
                 (140, 128, 108), bump=lambda x, z: 0.015*math.sin(3*x)*math.cos(2*z),
                 support_fn=hole_support)
    # back wall + side walls
    for (axis, sign, c) in [(2, 1, (150, 160, 170)), (2, -1, (140, 150, 158)), (0, 1, (120, 135, 145)), (0, -1, (120, 135, 145))]:
        i0 = len(verts); N = 40
        if axis == 2:
            for j in range(N + 1):
                for i in range(N + 1):
                    x = -3 + 6*i/N; y = 2.8*j/N; z = sign*2.5
                    verts.append((x, y, z)); cols.append(c); sup.append(0.0)
        else:
            for j in range(N + 1):
                for i in range(N + 1):
                    z = -2.5 + 5*i/N; y = 2.8*j/N; x = sign*3
                    verts.append((x, y, z)); cols.append(c); sup.append(0.0)
        for j in range(N):
            for i in range(N):
                a = i0 + j*(N+1) + i; b = a + 1; cc = a + N + 1; d = cc + 1
                faces += [(a, cc, b), (b, cc, d)]
    # table + ball + a column
    add_box(verts, faces, cols, sup, 0.8, 0.75, 0.3, 1.4, 0.08, 1.0, (96, 74, 50))
    for lx, lz in [(-0.6,-0.4),(0.6,-0.4),(-0.6,0.4),(0.6,0.4)]:
        add_box(verts, faces, cols, sup, 0.8+lx, 0.37, 0.3+lz, 0.06, 0.75, 0.06, (70, 55, 38))
    add_sphere(verts, faces, cols, sup, 0.8, 1.1, 0.3, 0.28, (210, 90, 50), support=0.02)
    add_box(verts, faces, cols, sup, -1.8, 0.45, -1.2, 0.5, 0.9, 0.5, (80, 95, 105))
    add_box(verts, faces, cols, sup, -1.8, 1.15, -1.2, 0.7, 0.5, 0.7, (90, 105, 115), support=0.05)

    write_ply(os.path.join(out, 'mesh.ply'), verts, cols, sup, faces)
    write_splat(os.path.join(out, 'splat.ply'), 160_000, rng)
    print(f"[fixtures] mesh.ply {len(verts)} verts {len(faces)} tris, splat.ply 160k -> {out}")

if __name__ == '__main__':
    main()
