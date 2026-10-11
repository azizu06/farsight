# seed.md — LiDAR-dot → Brush splat seed (T5)

`splat/brush/crates/farsight-seed`: turns the rover's dot cloud into a Brush-ready
Gaussian seed. Design constraint: **zero Brush changes** — the CLI emits an
INRIA-layout PLY that loads through the standard `ply_file_path` path in
transforms.json, plus a patched dataset dir that `brush-cli`/`farsight-splat`
can train on directly.

## Pipeline (`lib::run`)

| stage | what | file |
|---|---|---|
| clean | SOR (k=16, 2σ) + isolated-cluster removal (min 2 nbrs within 3× median spacing) | `clean.rs` |
| decimate | adaptive voxel merge to `target_splats` (voxel ×1.7 until ≤ target), weighted means | `decimate.rs` |
| frames | k=16-neighbourhood PCA: flat-disk tangents (spacing-scaled), thin normal axis, normal oriented toward nearest camera | `pca.rs` |
| colour | per-view **min-depth buffer** occlusion test; robust per-channel median of accepted photo samples (not the dot colour); coverage → opacity 0.5–0.8 | `colour.rs` |
| depth | same projection pass emits sparse inverse depth per view → `depth/` export for T2/T3 | `colour.rs`, `emit.rs` |
| fill | optional: completed-depth diffusion over photo-visible dot holes → low-opacity fill splats | `fill.rs` |

Emitted splat fields: `x,y,z` + `nx,ny,nz`(0) + `f_dc_*` (SH0, rgb→SH via
`(c/255−0.5)/SH_C0`) + `opacity` (logit) + `scale_0..2` (log) + `rot_0..3`
(w,x,y,z). 17 f32/vertex, 640×480-friendly.

## CLI

```
farsight-seed <capture_dir> <out_dir> [--target-splats N] [--thin F]
    [--scale-mul F] [--sor-k K] [--sor-std S] [--colour-stride N]
    [--depth-stride N] [--fill N]
farsight-seed-synth <out_dir> [n_views] [n_dots]   # test capture generator
```

`<out_dir>` = `transforms.json` (patched `ply_file_path`→`seed.ply`) +
`seed.ply` + `depth/` (sparse depth) + `images/` (hard-linked) +
`seed_stats.json`. One command; no Python.

## Sparse depth export (`depth/`)

`farsight.sparse_depth.v1`: `index.json` maps `view_NNNNNN.bin` →
transforms.json frame index; each file = `u32 count` then `count` records
`(f32 u, f32 v, f32 inv_depth)` in native image pixel coords, unoccluded
samples only. T2/T3 consume for the L1 depth loss with decaying weight.

## Correctness gates hit

- 5/5 unit tests (grid kNN = brute force, SOR drops outliers, decimate count
  bound, PCA normal on a flat patch, PLY 17-float layout).
- Brush loads `seed.ply` via `ply_file_path`, trains, and evals on lavapipe.

## Measured (lavapipe CPU-Vulkan, synthetic room capture — NOT the M5 numbers)

Test capture: `farsight-seed-synth` — 48 views 320×240, 150.5k dots
(±2 cm noise, 2 dot holes, 0.3% outliers), ARKit-like pose jitter, exposure
variation. 6 held-out eval views (eval-split-every 8).

Seed stage timings (150k dots, 16-core x86): clean+decimate+PCA+colour+emit
≈ **0.5–0.7 s total** — negligible vs one training step on the M5.

| config | steps | lavapipe wall | eval PSNR | SSIM |
|---|---|---|---|---|
| raw dots (init.ply as-is) | 200 | 93 s | 20.47 | 0.770 |
| farsight-seed | 200 | 112 s | **24.32** | **0.846** |

**+3.85 dB at equal step count.** Step-budget sweep (50/150/400/800) is
running; table updates on landing. Depth-loss row pending T2/T3 wiring —
export format is stable (`depth/` above), loss side unchanged.

### Notes / caveats

- Occlusion epsilon `occ_eps=0.06` (≈3σ of ±2 cm dot noise + pose jitter).
  Buffer at ds=1, dilate=1 — measured that coarser min-buffers mass-reject
  (ds=4/dilate=2 → 42% of in-frame projections accepted; ds=1/dilate=0 → 94%).
- Synth coverage: 79% of splats get ≥1 photo sample; the rest fall back to
  gray at opacity 0.5 (fill adds a few low-opacity splats in dot holes).
- `fill` is a stretch lane: on the synth (2 small holes) it adds ~7–30
  splats — cheap insurance, expect more on real captures with LiDAR holes.
- Numbers on lavapipe are correctness/relative evidence only; Mac PSNR-vs-s
  runs go through `splat/bench_mac.sh` (capture dir arg = the seeded out dir
  for the seeded arm, raw capture for the baseline arm).
