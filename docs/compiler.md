# farsight-compiler: point cloud → photoreal 3D environment (branch `compiler`)

`rust/farsight-compiler` is a Mac-side Rust compiler (rayon, all cores, no GPU
dependency, builds natively on Apple Silicon). It takes the rover's measured
cloud (`/dense/*`, `/map/dense.bin`, `/map/points.bin`, `/map/export.ply`,
`/geometry/mesh.bin`) plus, optionally, the post-run posed RGB capture
(`docs/postrun-capture.md`) and produces a hole-filled, textured environment:
`scene.glb` (PBR mesh + texture atlas), `splat.ply` (3DGS surfels, viewable at
once, also a Brush `init.ply` seed), `mesh.ply`, `points.ply`, `report.json`.

```sh
cd rust && cargo build --release -p farsight-compiler
./target/release/farsight-compile synth --out synth            # ground-truth test room
./target/release/farsight-compile bench --preset balanced      # synth -> compile -> metrics
./target/release/farsight-compile compile --input http://127.0.0.1:8080 \
    --capture ~/farsight-captures/<run> --out compiled --preset ultra
```

## Pipeline (one module per stage, one owner per module)

| Stage | Module | What it does |
|---|---|---|
| ingest | `io/` | all farsight wire formats + PLY + live pull + nerfstudio capture |
| clean | `clean.rs` | merge, statistical/radius outliers, flying pixels |
| normals | `normals.rs` | PCA normals, consistent orientation, local spacing |
| densify | `densify.rs` | **interpolation**: MLS projection/upsampling, gap bridging, colour interpolation (image-guided) |
| surface | `surface.rs` | sparse implicit surface from oriented points, marching cubes, trim unsupported fill |
| meshops | `meshops.rs` | cleanup, hole fill, smoothing, colour transfer, decimation |
| texture | `texture.rs` | projective texture atlas from posed images, seam/exposure blending |
| export | `export/` | GLB, 3DGS splat PLY, PLY |
| viewer | `tools/compiler_viewer/` | three.js PBR viewer (mesh + splat), measured/interpolated toggle |

Shared types live in `core.rs` (`PointCloud`, `Mesh`, `Camera`, `Capture`,
`Image`); stage orchestration in `pipeline.rs`; presets `fast|balanced|ultra`.
Interpolated geometry is always labelled: points carry `FLAG_INTERPOLATED`,
mesh vertices carry `support` (distance to nearest measured sample).

## Ground truth and metrics

`synth.rs` ray-traces a furnished 6×2.8×5 m room from a rover-height path:
noisy depth (σ = 2 mm + 1.5 mm·d²), grazing dropout, 10 mm `/dense`
quantisation, occlusion shadows and four deliberate unobserved holes, plus
640×480 posed RGB images in the `run_capture.py` layout. `bench` reports
precision / recall / F-score at τ = 2 cm (Tanks&Temples style), **hole recall**
(true surface inside the holes that the output covers — the interpolation
score) and mesh vertex-colour error, for the input, densified cloud and mesh.
These are synthetic numbers; they do not prove behaviour on rover data.
