# splat-turbo perf notes (T7: brush-loss / lpips / workspace config)

Scope: `splat/brush/crates/brush-loss`, `lpips`, workspace Cargo profiles/features,
plus the render-pipeline sync removal this task needed (touches brush-render /
brush-sort / brush-train — coordinated as part of task (1)).

## Step timeline (before this change)

One `SplatTrainer::step` on the cubecl/wgpu backend looked like:

```
host:        [dataset batch] [PF launch] [8B readback] [15 launches] [adam] [noise]
GPU:                    ...PF...  ^drain^    sort/gather/prefix/project/map/tilesort/tileoffs/rasterize ... loss ... bwd ... adam ...
```

The readback of `num_visible` + `num_intersections` (two u32 atomics written by
`project_forward`) forced the GPU to drain after project_forward and the host to
sleep inside `render()` until it finished. Every buffer/dispatch below PF was
then sized by those host numbers.

## What changed (commit series on `splat-turbo`)

- `kernels::counts::write_counts`: a 1-thread kernel right after
  `project_forward` packs the two atomics into `counts = [num_visible,
  min(num_intersections, isect_cap)]` on-GPU.
- All downstream launches are sized by **capacity** (`total_splats`, `isect_cap`)
  and bound in-kernel:
  - `project_visible`, `map_gaussians`: launch over `total_splats`, early-out
    `compact_gid >= counts[0]`.
  - `map_gaussians` also clamps isect writes to `isect_cap` (overflow tail is
    dropped for that step instead of scribbling).
  - `radix_argsort_len` (new in brush-sort): capacity-sized buffers +
    GPU-count `num_keys`; the sort kernels already early-out extra workgroups.
  - `get_tile_offsets`: launch over `isect_cap`, bound `counts[1]` in-kernel.
  - `prefix_sum`/`int_gather` run on capacity tensors; garbage tail values are
    never read (all consumers index `< counts[0]`).
- `isect_cap = min(total_splats*8, total_splats*num_tiles)`: bounded memory
  (2×4B×cap). If it ever binds, `counts[1] < num_intersections` shows in the
  deferred stats read; raise the factor 8.
- `project_uniforms.num_visible = total_splats` (capacity semantics; bwd's
  `v_combined` rows derive from it — consistent).
- The only remaining per-step GPU->CPU read is the stats read of `counts`
  (8 bytes) in `train.rs`, deferred until forward+loss+backward+adam+noise are
  queued, so it overlaps real GPU work. It feeds `TrainStepStats.num_visible`
  only.
- `aux.num_visible/num_intersections` (u32) became `aux.counts`
  (`Tensor<1, Int>`) in `RenderAux`/`RenderAuxInner`/`SplatOutputDiff`; tests
  read it explicitly.

## Other syncs in the loop (surveyed, unchanged)

| Site | Cadence | Notes |
|---|---|---|
| `refine()` argwhere/counts reads | `refine_every` | batch-able; next target |
| eval psnr/ssim scalars | `eval_every` | fine |
| export | `export_every` | fine |
| rerun `log_train_stats`/`log_splats` | rerun-connected only | reads loss scalar etc; off when no viewer |

## Apple build config (commit cba1c26 era)

- `[profile.release]`: `lto = "fat"`, `codegen-units = 1`, `panic = "abort"`,
  `strip`, `opt-level = 3`. Host-side dispatch is a real share of a step on
  wgpu; fat LTO + 1 CU maximises cross-crate inlining.
- `.cargo/config.toml`: `target-cpu = native` for `aarch64-apple-darwin`.
- `brush-cube/metal` feature → `burn-wgpu/metal` → cubecl `wgpu-msl`: MSL
  direct codegen, skips naga WGSL->MSL (time-to-first-step + dispatch).
  Requires Apple7+ GPU, Metal >= 3.2 (M5 Pro OK).
- cubecl autotune cache: `autotune_persistence` compiles in for std+desktop;
  cache lives under `user_cache_dir()/cubecl` — keep the same working dir /
  user across bench runs so the 60 s budget doesn't pay for tuning.
- wgpu on macOS also gets `vulkan-portability` via cubecl-wgpu target cfg;
  wgpu picks the Metal adapter at runtime on the M5 Pro regardless.

## Fused L1 + D-SSIM (task 2)

Already vendored: `crates/brush-loss/src/lib.rs` has
`image_loss_forward_kernel`/`image_loss_backward_kernel` (16x16 fwd / 8x8 bwd
tiles, HALO=5, 11-tap Gaussian, partials per tile) — used on the train path.
TODO on the Mac: verify Metal perf vs. burn tensor ops, tune block sizes for
simdgroup width 32.

## Verification

- `cargo check -p brush-render -p brush-sort -p brush-train -p brush-bench-test --tests`: clean.
- lavapipe before/after + synth-capture PSNR-vs-seconds ablation: pending
  T1's `farsight-splat synth` capture; bench via `splat/bench_mac.sh` on the Mac.
