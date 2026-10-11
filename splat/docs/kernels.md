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

TBD — pending bench harness.

## Change log

(Each entry: change, lavapipe before→after, expected Mac effect, commit SHA.)
