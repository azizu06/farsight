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

## Measured (lavapipe, Mesa CPU Vulkan, 16-core x86 VM)

Toy capture: 24× 160x120 PNG frames + 400-dot init.ply (`/tmp/t7_synth`,
deterministic). Same binary command both runs:
`brush-cli /tmp/t7_synth --total-train-iters 100 --eval-every 100
--eval-split-every 8 --export-path <out>/`

| build | end-to-end | train loop | eval ssim |
|---|---|---|---|
| before sync removal (binary built 11:53) | 15 s | 12 s | 0.00008758852 |
| after (aadc4ec + aa29f42, fat-LTO build) | 9 s | 8 s | 0.00008758852 |

-40% end-to-end on the toy; identical ssim bits = change is
numerics-preserving. Caveat: the "after" run had a warm autotune cache
(5 entries), and on a real 1.45M-splat scene the mid-forward bubble is
larger relative to step time. Real PSNR-vs-seconds ablation still pending
T1's `farsight-splat synth` capture (not pushed yet); on the Mac it runs
via `splat/bench_mac.sh` end to end.

## `.splat` export (task 5, commit aa29f42)

`brush_serde::splat_to_ply_and_splat` — one `Transaction` readback ->
`.ply` + packed antimatter15 `.splat` (32 B/splat:
pos f32x3, exp scale f32x3, rgba u8, quat u8x4 normalized*128+128,
sorted by exp(volume)*sigmoid(opacity) desc — byte-compatible with
`tools/postprocess/ply2splat.py`). `export_checkpoint` writes
`export_{iter}.splat` beside the .ply; `tools/postprocess/walk.html`
renders it directly (`?scene=<url>`, `SceneFormat.Splat`). `cargo check`
clean; the after run predates this commit so only `export_100.ply` was
observed on disk — first .splat lands on the next export run.

## Verification

- `cargo check -p brush-render -p brush-sort -p brush-train -p brush-bench-test --tests`: clean.
- lavapipe before/after + synth-capture PSNR-vs-seconds ablation: pending
  T1's `farsight-splat synth` capture; bench via `splat/bench_mac.sh` on the Mac.

## Time-to-first-step + caches (f40722b, warmup + CUBECL_CACHE_DIR)

- `brush-cli` main calls `cubecl_environment::environment::set_root` with
  `CUBECL_CACHE_DIR` else `~/.cache/farsight-splat/cubecl` — the autotune
  cache (and cubecl's other persisted state) lands in one predictable dir
  the Mac bench can snapshot/clear. wgpu has no portable disk pipeline
  cache; on Metal the OS caches compiled pipelines itself.
- `--warmup` (TrainConfig) runs one real step + one `refine(0)` before the
  training loop — compiles every kernel the loop touches at the REAL
  image/splat sizes, including the grow/prune kernels that otherwise pay
  their compile cost inside the budget window at the first refine tick.
- Lavapipe cold vs warm (toy 160x120, iters=1): cold ~1 s, warm ~2 s,
  warm+`--warmup` +4.5 s (one step + refine at these sizes). At toy scale
  autotune barely matters — the value is at 300k splats/1920x1440 where
  first-compile of the loss/rasterize/sort kernels costs seconds inside
  the 60 s window. Mac run: `bench_mac.sh` exports CUBECL_CACHE_DIR and
  passes `--warmup`; compare a cleared `~/.cache/farsight-splat` first run
  against the second to see the warmup amortization.

## `--profile <file>` per-kernel GPU table (620a9c2)

`ProcessConfig::profile` installs a cubecl `LaunchObserver` with
`TimingRequest::Resolved`: every kernel gets device timestamps, dumped to
JSON `{kernel: launches, total_ms, mean_ms, methods}` on DoneTraining.
Resolved waits per kernel → serializes the queue — profiling runs only,
NEVER a timed budget run. `methods.device` vs `methods.system` separates
real device timestamps from cubecl's host-clock fallback (wgpu timestamp
budget spent, or backends without queries).

Lavapipe, 20-iter toy run (160x120, 400 splats), top of 33 kernels:

| kernel | launches | total_ms | ms/launch |
|---|---|---|---|
| `image_loss_backward_kernel<f32>` | 20 | 341 | 17.05 |
| `elemwise_fuse` (fused burn ops) | 329 | 110 | 0.34 |
| `image_loss_forward_kernel<f32>` | 26 | 103 | 3.97 |
| `rasterize_kernel` | 23 | 97 | 4.23 |
| `sort_scan_add_kernel` | 115 | 93 | 0.81 |

**Loss backward already dominates on lavapipe — at 1920x1440 (~144x the
pixels) it is the #1 per-step cost on the M5 Pro run; that's exactly what
`ssim_bench` + the tile-variant features are for.** Mac kernel table:
`--profile kernels.json` on any bench run.

## MSL-direct feature `metal-msl` (f40722b)

`brush-cli/metal-msl` → `brush-process` → `brush-render`/`brush-train` →
`brush-cube/metal` (MSL codegen instead of WGSL→transpile for cube
kernels). Default ON only for `aarch64-apple-darwin` via
`splat/bench_mac.sh` (uname gate); everything else keeps wgpu/WGSL — CI
and lavapipe need WGSL (the metal path has real MSL codegen, still
experimental in cubecl; the u8-bool kernel paths would fail on non-Metal
drivers). Linux `cargo check` with the feature passes.
Mac run must check: (a) identical eval PSNR vs a WGSL run at same iters —
any drift is an MSL codegen bug, report it; (b) first-run vs second-run
cold time (MSL skips WGSL→transpile so time-to-first-step drops);
(c) kernels.json `methods` should stay `device`.

## Eval + cadence syncs (bff8686, 84bd953)

- run_eval kept `into_scalar().await` per view — 90 held-out views = 90
  serialized GPU drains. Now scalars accumulate as GPU tensors during the
  render loop and read back once via `Transaction::execute_async` — the
  GPU stays queued across all eval renders, one read at the end.
- Refine's `argwhere_async`/`into_data_async` calls are already issued
  async; the await is in-place because the indices feed `select`/`scatter`
  in the same call — they can't be deferred without a cross-step refine
  state machine. They're cadence-gated (every `refine_every` steps), so
  the residual stall is once per refine tick, not per step.
- Stats/bookkeeping reads (`counts`, `splat_dist` percentiles) already
  defer to end-of-step or use `into_data_async`.

## Unified-memory image uploads (Mac)

T6's `img_gpu` uploads go through `queue.write_buffer` into cubecl's
device buffers. On Apple unified memory wgpu's Metal backend allocates
shared-storage buffers, so the "staging copy" is a host memcpy — there is
no PCIe transfer to remove, and wgpu's public API exposes no faster
zero-copy path for `write_buffer`. The remaining Mac lever is not the
copy but the *count*: `img_gpu` is populated once at scene load (not per
step), so per-step cost is already zero-copy-by-omission. Verify on Mac:
`--profile` shows no upload kernels mid-step; the only H2D traffic is at
startup.

## SSIM tile variants (20c137c / 06416a3)

`brush-loss` feature flags pick kernel tile consts:
`ssim-fwd-32x8` (32-wide rows = one full simdgroup per row on M5),
`ssim-fwd-8x8`, `ssim-bwd-8x4` (smaller bwd tile for Apple's 32 KiB
threadgroup, where cubecl-wgpu's shared-mem check double-counts).
Default stays 16x16 fwd / 8x8 bwd. Bench per variant:
`cargo test --release -p brush-loss --features <variant> ssim_bench -- --ignored --nocapture`
— times fwd and fwd+bwd at 480x640 and 1200x1600 and prints ms/call.
