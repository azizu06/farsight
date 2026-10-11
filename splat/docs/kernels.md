# splat-turbo kernel notes (T2 lane)

Scope: `splat/brush/crates/brush-render/src/{kernels,bwd}`, `brush-sort`, `brush-scan`.
Goal: make each train step's forward+backward render as fast as possible on Apple
Metal (M5 Pro, simdgroup 32) with results unchanged within float tolerance.
Numbers here are measured on this VM under Vulkan **lavapipe** (CPU) unless marked
otherwise; they are for *relative* before/after comparisons only. Absolute speed is
validated on the Mac via `splat/bench_mac.sh`.

## Per-step dispatch inventory (baseline, vendored 1388f74)

One train step runs `SplatOps::render` (forward, `bwd_info=true`) then the two
backward kernels. Forward launches, in order:

| # | Kernel | Work items | Notes |
|---|--------|-----------|-------|
| 1 | `project_forward` | total_splats | culls, counts contributing tiles per splat, atomics `num_visible`/`num_intersections` |
| 2 | **CPU readback** | 2×u32 | `num_visible`+`num_intersections` — pipeline stall inside every step |
| 3 | `radix_argsort` depth | num_visible keys | 32-bit f32 keys (positive floats sort as u32) → **8 passes × 5 launches = 40 dispatches** |
| 4 | `int_gather` compact_counts | num_visible | burn gather |
| 5 | `prefix_sum` cum_tiles_hit | num_visible | `scan_blocks` + levels + `add_block_offsets` |
| 6 | `project_visible` | num_visible | writes 9-lane `projected_splats` |
| 7 | `map_gaussians_to_intersect` | num_visible | re-walks tile bbox, emits (tile_id, compact_gid) pairs |
| 8 | `radix_argsort` tile_id | num_intersections | `ceil(bits/4)` passes × 5 launches; 640×480 → 1200 tiles → 11 bits → **3 passes = 15 dispatches** |
| 9 | `get_tile_offsets` | num_intersections/8 | boundary scan of sorted tile ids |
| 10 | `rasterize` | num_tiles × 256 thr | per-tile alpha compositing |

Backward:

| # | Kernel | Work items | Notes |
|---|--------|-----------|-------|
| 11 | `rasterize_backwards` | num_tiles × 32 thr | per-splat grad in registers, 10 atomic f32 adds per splat-batch |
| 12 | `project_backwards` | num_visible + 1 | dense VJP |
| — | gathers into dense grads | 4 × float_select | in `burn_glue` backward |

## Baseline observations

- **Dispatch count is the smell.** 40 launches for depth sort + 15 for tile
  sort + ~10 for scan/offsets ≈ 65+ small dispatches per step, before rasterize
  even starts. On Metal each wgpu dispatch costs ~10-20 µs of encode+scheduling;
  that alone is ~1 ms/step of overhead.
- The CPU readback at step 2 is a hard sync: it waits for `project_forward`
  before any later dispatch is even *built*. Everything after it is serial
  CPU-time encoding.
- `count_contributing_tiles` (exact StopThePop rect-vs-ellipse test +
  opacity-aware bound `ln(opac·255)`) already gives near-SnugBox footprints —
  it runs **twice** per splat (PF count pass + MG emit pass).
- Rasterize fwd: 256 threads/tile, shared-mem batch staging, per-batch
  workgroup barrier; early-exit via `num_done_atomic`.
- Rasterize bwd: diagonal schedule, SPLAT_BATCH=32 = one simdgroup, register
  accumulation, 10 atomic adds per splat per tile-batch (already the
  Taming-3DGS/gsplat-packed structure; remaining win is fewer atomics and
  fewer barrier stalls).
- Tile sort keys are only `ceil(log2(num_tiles))` bits — already minimal at
  4-bit digits. The lever is pass count (bigger digits) or a packed
  (tile_id, depth) single sort.
- Depth sort on 32-bit f32 keys = max 8 passes. Splats only need *order*;
  depth quantization trades exact order for pass count.

## Timings (lavapipe, relative)

Harness: `crates/brush-render/examples/pipeline_prof.rs` —
`BRUSH_PROFILE=1 cargo run --release -p brush-render --example pipeline_prof`
(sync-bracketed per-phase ms via `profile.rs`; phase sums include sync bubbles,
compare like-for-like only). Scene: 400k synthetic room splats, 640×480
(1200 tiles → 11-bit tile sort), 234k visible, 5.87M intersections (~25/splat).
Numbers = avg of 3 iters after warmup.

| Phase | before ms | after 8-bit digits |
|-------|----------:|-------------------:|
| project_forward | 38.2 | 27.9 |
| readback | 0.6 | 0.5 |
| depth_sort | 108.5 | 48.9 |
| gather+scan | 3.7 | 2.4 |
| project_visible | 7.9 | 4.6 |
| map_gaussians | 52.6 | 56.1 |
| tile_sort | 857.2 | 445.2 |
| tile_offsets | 6.0 | 4.0 |
| rasterize (fwd) | 145.0 | 95.1 |
| phase_sum | 1220 | 685 (-44%) |
| wall fwd+bwd | 1478 | 840 (-43%) |

(bwd phases — `rasterize_bwd`, `project_bwd` — run on burn's autodiff
pool, outside the profiled thread; the wall delta contains them + gathers.)

## Change log

(Each entry: change, lavapipe before→after, expected Mac effect, commit SHA.)

- **brush-sort: 4-bit → 8-bit digits** (commit below). Tile sort 3→2 passes,
  depth sort 8→4 passes; histogram table 16→256 bins (count_buf = wgs×256,
  still ≤ ~1/4 of sorted data at 5.9M keys). Scatter bin-scan rewritten as one
  wg-wide `cube_exclusive_sum` (replaces plane-scan/serial paths, works for any
  BIN_COUNT ≤ WG). Measured: sort time ≈ −50% both sorts; fwd total −44%.
  Same tests pass (sort 3/4; render 32/34 — two big-buffer tests fail pre-change
  too: lavapipe host-memory reservation limit). On the M5 the same pass halving
  applies; element traffic is the dominant sort cost there too.
- **rasterize_backwards: tile-level early exit** (`bb2989f` → relanded
  `511f55d`). Shared `num_done` atomic counts saturated pixels
  (`next_t <= 1e-4`, or outside the image); the batch loop votes
  `workgroup_uniform_load_atomic(num_done) >= TILE_SIZE` at loop top and
  breaks — remaining splat batches contribute provably-zero gradient.
  First landing failed lavapipe/Mesa-23.2 SPIR-V validation
  (`Use of v265 not dominated by definition`) because the uniform-load
  (a barrier op) sat inside the `||` short-circuit of the while
  condition — barrier in non-uniform control flow → undominated SSA.
  Reverted in `98a7205`, relanded with the vote hoisted unconditionally
  to loop top (same pattern as the fwd kernel's `num_done_atomic`
  check), plus a `sync_cube` ordering the `num_done` store before the
  outside-pixel adds. This WAS the cause of the fuzz_bwd failures and
  the `train.rs:913 counts dtype` panic on llvmpipe — NOT vendored
  code (T7 bisect). Regression test:
  `bwd_early_exit_vote_runs_under_real_variant` drives both vote
  outcomes through `bwd::render_splats` (real `HfAtomicAdd`/
  `CasAtomicAdd` variant selection). Note for cubecl work: never put a
  barrier-containing op (`workgroup_uniform_load*`, `sync_cube`,
  plane ops) inside a short-circuit operand or a non-uniform `if`.
- **adaptive isect capacity — no more silent isect drops** (`a7b4951`).
  `counts` is now `[num_visible, isect_used, isect_total]`; `seal_isects`
  writes the unclamped total to `counts[2]` for free (it reads the last
  cumsum element anyway). Capacity each step =
  `ceil(prev_total * 1.25)` grow-only, fed by the train loop's already
  deferred counts readback — zero new mid-step syncs. Step 0 does one
  sync read of `counts[2]` for exact sizing. At 400k-splat/640×480
  synthetic density the true count is 5.87M vs the old `8×splats` = 3.2M
  cap → 2.7M isects/step were silently dropped (missing tiles). Test
  `high_density_scene_drops_no_intersections` asserts
  `counts[1] == counts[2]` on exact + hinted renders.
- **SUBGROUP fallback in scan/sort** (`5fa41c1`). llvmpipe-via-GL has no
  `SUBGROUP` capability; `cube_exclusive_sum`'s `plane_inclusive_sum`
  emitted `subgroupInclusiveAdd` and killed kernel validation
  (train.rs:913 again — same panic, different root cause). All scan
  primitives take `#[comptime] use_planes`; launch sites query
  `client.properties().features.plane.contains(Plane::Ops)` once and
  pass it through. Fallback = Hillis-Steele shared-mem scan
  (log2(CUBE_DIM) rounds, 2 syncs/round — slower but correct on any
  adapter). `test_scan_blocks_no_planes` forces `use_planes=false` and
  validates against CPU scan.
- **Known lavapipe limit**: `brush-sort::test_sorting_above_scan_block_size`
  fails reserving a 280 MB buffer — llvmpipe device-memory cap, fires
  before any kernel launch; environmental, not a regression. Re-check on
  the M5.
- **Mac check item**: rerun `fuzz_bwd_*` + `test_scan_blocks_no_planes`
  on Metal (MSL path, not SPIR-V) to confirm both the early-exit vote
  and the use_planes gate behave there.
