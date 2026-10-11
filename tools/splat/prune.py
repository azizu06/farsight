"""Cap a 3D Gaussian splat .ply for Quest Browser: keep the most visible splats, trim SH.

    python -m tools.splat.prune in.ply out.ply [--max-splats 300000] [--sh-degree 1]

Importance = sigmoid(opacity) * exp(scale_0 + scale_1 + scale_2), i.e. opacity times
volume, the usual 3DGS pruning score. Output properties are written in the canonical
3DGS order (x y z, f_dc_*, f_rest_* channel-major, opacity, scale_*, rot_*) so loaders
that read by position or by name both work.
"""

import argparse
import json
import sys

import numpy as np

CANONICAL = ["x", "y", "z", "nx", "ny", "nz", "f_dc_0", "f_dc_1", "f_dc_2"]
TAIL = ["opacity", "scale_0", "scale_1", "scale_2", "rot_0", "rot_1", "rot_2", "rot_3"]


def read_ply(path):
    with open(path, "rb") as f:
        header, comments, names, count = [], [], [], None
        while True:
            line = f.readline()
            if not line:
                raise ValueError("PLY header has no end_header")
            text = line.decode("ascii").strip()
            header.append(text)
            if text.startswith("format") and text != "format binary_little_endian 1.0":
                raise ValueError(f"Unsupported PLY format: {text}")
            if text.startswith("comment"):
                comments.append(text)
            elif text.startswith("element vertex"):
                count = int(text.split()[2])
            elif text.startswith("element"):
                raise ValueError(f"Unsupported PLY element: {text}")
            elif text.startswith("property"):
                kind, name = text.split()[1:3]
                if kind != "float":
                    raise ValueError(f"Only float properties are supported, got {text}")
                names.append(name)
            elif text == "end_header":
                break
        data = np.fromfile(f, dtype="<f4", count=count * len(names))
    if count is None or data.size != count * len(names):
        raise ValueError("PLY vertex data is truncated")
    return comments, names, data.reshape(count, len(names))


def sh_degree(names):
    rest = sum(name.startswith("f_rest_") for name in names) // 3
    return int(round((rest + 1) ** 0.5)) - 1


def prune(names, rows, max_splats, max_sh_degree):
    column = {name: i for i, name in enumerate(names)}
    keep = np.arange(len(rows))
    if 0 < max_splats < len(rows):
        opacity = 1 / (1 + np.exp(-rows[:, column["opacity"]].astype(np.float64)))
        volume = np.exp(sum(rows[:, column[f"scale_{i}"]].astype(np.float64) for i in range(3)))
        keep = np.sort(np.argpartition(-(opacity * volume), max_splats - 1)[:max_splats])
    rows = rows[keep]
    degree = sh_degree(names)
    target = degree if max_sh_degree < 0 else min(degree, max_sh_degree)
    per_channel_in, per_channel_out = (degree + 1) ** 2 - 1, (target + 1) ** 2 - 1
    # f_rest is channel-major: [R coeffs..., G coeffs..., B coeffs...].
    rest = [f"f_rest_{c * per_channel_in + k}" for c in range(3) for k in range(per_channel_out)]
    order = ([n for n in CANONICAL if n in column] + rest
             + [n for n in TAIL if n in column]
             + [n for n in names if n not in CANONICAL and n not in TAIL and not n.startswith("f_rest_")])
    renamed = {name: f"f_rest_{i}" for i, name in enumerate(rest)}
    out_names = [renamed.get(n, n) for n in order]
    return out_names,np.ascontiguousarray(rows[:, [column[n] for n in order]], dtype="<f4"), target


def write_ply(path, comments, names, rows, degree):
    comments = [c for c in comments if not c.startswith("comment SH degree")] + [f"comment SH degree: {degree}"]
    header = ["ply", "format binary_little_endian 1.0", *comments, f"element vertex {len(rows)}",
              *[f"property float {n}" for n in names], "end_header"]
    with open(path, "wb") as f:
        f.write(("\n".join(header) + "\n").encode("ascii"))
        rows.tofile(f)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--max-splats", type=int, default=300_000)
    parser.add_argument("--sh-degree", type=int, default=1, help="Highest SH degree kept; -1 keeps all")
    args = parser.parse_args(argv)
    comments, names, rows = read_ply(args.input)
    out_names, out_rows, degree = prune(names, rows, args.max_splats, args.sh_degree)
    write_ply(args.output, comments, out_names, out_rows, degree)
    json.dump(dict(input_splats=len(rows), output_splats=len(out_rows), input_sh_degree=sh_degree(names),
                   output_sh_degree=degree, bytes=out_rows.nbytes), sys.stdout)
    print()


if __name__ == "__main__":
    main()
