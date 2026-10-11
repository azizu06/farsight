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

## Post-T7 sync-free pipeline (lavapipe)

Same scene/harness after merging T7's device-side counts
(`write_counts`, capacity buffers, `radix_argsort_len` GPU bound):

| Phase | ms |
|-------|---:|
| project_forward | 39.0 |
| write_counts | 0.4 |
| depth_sort | 78.4 |
| gather+scan | 4.9 |
| project_visible | 7.4 |
| map_gaussians | 90.4 |
| tile_sort | 421.1 |
| tile_offsets | 3.5 |
| rasterize (fwd) | 141.0 |
| phase_sum | 786 |
| wall fwd+bwd | 1093 |

Note: `isect_cap = min(8×total_splats, total×num_tiles)` = 3.2M **binds** on
this scene (5.87M actual isects) — tail intersections are silently dropped.
depth_sort/map_gaussians regressions vs the pre-merge run are capacity-launch
overhead (wgs sized to total_splats, early-out on `counts[0]`).

## Change log

(Each entry: change, lavapipe before→after, expected Mac effect, commit SHA.)

- **render: drop per-splat `num_intersections` atomic** (`6324ce0`). The
  inclusive prefix scan already yields the isect total as its last element;
  `seal_isects` (single-cube launch after `prefix_sum`) writes
  `counts[1] = min(total, cap)`. Removes ~`num_visible` global atomic adds
  per step from `project_forward`.
- **rasterize: dedupe `visible[]` writes** (`6324ce0`). `bwd_info` path used
  to write `visible[gid]=1` once per contributing (pixel, splat) pair
  (~1.5B writes on the 5.87M-isect scene). Now a `TILE_SIZE/32`-word shared
  bitmap collects marks, flushed once per splat per tile (~isects writes,
  identical result). 32/32 render tests pass.
- **rasterize_backwards: tile-level early exit** (below). Shared `num_done`
  counter: pixels count as done when `next_t <= 1e-4` (or start outside the
  image); the batch loop exits once all 256 saturate — remaining batches
  provably contribute zero gradient. Saturated-room tiles skip most of
  `rounds × (SPLAT_BATCH+255)` iterations. 32/32 render tests pass;
  autodiff train test green.

- **brush-sort: 4-bit → 8-bit digits** (commit below). Tile sort 3→2 passes,
  depth sort 8→4 passes; histogram table 16→256 bins (count_buf = wgs×256,
  still ≤ ~1/4 of sorted data at 5.9M keys). Scatter bin-scan rewritten as one
  wg-wide `cube_exclusive_sum` (replaces plane-scan/serial paths, works for any
  BIN_COUNT ≤ WG). Measured: sort time ≈ −50% both sorts; fwd total −44%.
  Same tests pass (sort 3/4; render 32/34 — two big-buffer tests fail pre-change
  too: lavapipe host-memory reservation limit). On the M5 the same pass halving
  applies; element traffic is the dominant sort cost there too.
