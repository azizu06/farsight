# splat-turbo: optimizer & schedule (T3)

Everything the trainer does is driven by one scalar — the **schedule fraction**
`progress ∈ [0,1]`:

- `--budget-s S` set → `progress = elapsed_wall / S` (wall clock starts at the
  first train step, so dataset loading is free).
- Otherwise → `progress = iter / total_train_iters` (upstream behaviour).

Because every schedule keys off `progress`, a `--budget-s 60` run and a
`--total-train-iters 30000` run both execute the *complete* schedule — the
budget just samples it coarser. `train_stream` breaks the loop when
`trainer.budget_exhausted()` goes true and still runs the final eval + export
(`is_last_step` covers both endings).

| Driven by `progress` | Flag | Default |
|---|---|---|
| Mean-LR decay `lr_mean → lr_mean_end` | `lr_mean`, `lr_mean_end` | 2e-5 → 2e-7 |
| Resolution ramp | `--res-start-div`, `--res-full-at` | 1 (off), 0.6 |
| SH band unlock | `--sh-unlock-at` | empty (all live) |
| Refine gating | `refine_every` × progress ≤ 0.95 | 200 iters |
| Growth window | `growth_start_iter`, `growth_stop_iter` (as fractions of `total_train_iters`) | 0 → 15000 |
| Min-scale floor freeze | `MIN_SCALE_FREEZE_FRAC` | 0.9 |
| Opacity decay per refine | `--opac-decay` × (1 − progress) | 0.004 |
| Loop stop | `budget_exhausted()` | — |

## Progressive resolution (DashGaussian)

`--res-start-div 4 --res-full-at 0.6`: GT is box-downscaled on the host
(per-channel average over div×div blocks — better than strided subsample),
the camera renders at the smaller `img_size` (FOV intrinsics auto-scale), so
the whole render+loss pipeline sees ~16× fewer pixels at start. Divisor halves
at even intervals and hits 1 at 60% of the schedule. At full res the loader's
GPU-resident `img_gpu` fast path is used unchanged.

## Sparse / visible-only Adam (`--sparse-adam`)

Taming-3DGS SparseAdam: one argwhere on `visible` (splats that actually
contributed pixels — union across the step's views), then per param:
`select(0, inds)` → Adam on compact rows → `select_assign` back. Momentum for
invisible rows is left untouched (no decay toward zero); never-seen rows
step with zero-init moments, same as a dense first step. `coeffs_grad_sq`
(the reduced SH second moment) is selected to match. One small D2H sync per
step — nearly free on unified memory (Metal).

## LiDAR-seeded LRs

Ply-seeded runs auto-enable (`set_seeded(init_splat.is_some())`):
- `--seeded-mean-lr-scale` (1.0): multiplies the scheduled mean LR — the seed
  already carries accurate geometry.
- `--seeded-early-boost` × `--seeded-boost-frac` (1.0, 0.15): boosts
  `lr_coeffs_dc` and `lr_opac` early — geometry is free, spend the start of
  the budget on appearance.

## SH unlock (`--sh-unlock-at f1,f2,...`)

`sh_unlock_at[d]` = progress fraction when SH degree d+1 unlocks (DC always
live). Implemented as a cached per-coefficient Adam *scaling* mask — no
coefficient re-materialization, no optimizer-state surgery when a band opens
(its moments were frozen at zero-init and are consistent).

## Multi-view batch (`--views-per-step N`)

`step_multi(&[SceneBatch])`: renders N views, averages the losses, one
optimizer step. Aux merges: `visible`/`opacities`/`max_radius` take the
element-wise max (union of contributing splats); refine weights accumulate
per view. At fixed *view* budget, N>1 means fewer optimizer steps and fewer
refine events — the honest trade the ablation measures.

## Ablations

All on the local lavapipe (CPU Vulkan) box — relative numbers only; absolute
speed counts on the Mac via `splat/bench_mac.sh`. Capture: provisional numpy
synth (60 f, 320×240, orbit views, 47.6k-pt noisy ply seed) until
`farsight-splat synth` lands. Fixed budget 1200 views/run.

| Config | Extra flags | Iters | Wall | PSNR | Splats |
|---|---|---|---|---|---|
| stock | — | 1200 | 217 s | 21.99 | 37,637 |
| stock (traj) | eval@300/600/900/1200 | 1200 | — | 6.31/6.51/16.66/22.19 | 37,637 |
| sparse adam | `--sparse-adam` | 1200 | 193 s | 6.45 † | 37,011 |
| sparse all-inds | `--sparse-adam` `BRUSH_SPARSE_ALL=1` | 1200 | — | **21.98** (control) | — |
| prog res | `--res-start-div 4 --res-full-at 0.6` | 1200 | 159 s | **22.64** | 25,118 |
| sh 0→1 | `--sh-degree 1 --sh-unlock-at 0.3` | 1200 | 188 s | 22.04 | 37,367 |
| sh deg3 unlock | `--sh-degree 3 --sh-unlock-at 0.15,0.3,0.5` | 1200 | 247 s | 22.02 | 36,384 |
| seeded LR deep | `--seeded-mean-lr-scale 0.5 --seeded-early-boost 2.0` | 1200 | 220 s | 21.18 | 35,621 |
| seeded LR deeper | `--seeded-mean-lr-scale 0.2 --seeded-early-boost 3.0` | 1200 | 226 s | 19.95 | 32,276 |
| views×4 | `--views-per-step 4` | 300 | 192 s | 6.32 ‡ | 48,093 |
| no opac decay | `--opac-decay 0` | 1200 | 247 s | 21.96 | 37,763 |
| mip off | `--min-scale-factor 0` | 1200 | 271 s | 21.81 | 54,778 |
| mip 0.3 | `--min-scale-factor 0.3` | 1200 | 253 s | 21.65 | 23,393 |
| ssim 0 | `--ssim-weight 0` | 1200 | — § | 21.89 | 37,319 |
| ssim 0.5 | `--ssim-weight 0.5` | 1200 | — § | **22.13** | 35,222 |
| ema 0.999 | `--ema-decay 0.999` | 1200 | — § | 13.45 ⁑ | 36,950 |
| combo (bad) | sparse+res+sh+seed | 1200 | 124 s | 6.49 † | 26,112 |

‡ views×4 at a fixed *view* budget does ¼ the optimizer steps — starves like
sparse (flatline at 6.32). The honest comparison is equal *step* budget; the
result there still trades gradient freshness for step count — see below.
⁑ EMA 0.999 lags ~1000 steps on a 1200-iter run — use ≤0.95 (window ≈ 20
steps); sweep in progress. § walls from contended lavapipe are inflated;
PSNR unaffected, wall deltas count only on Mac.

† sparse-adam measured-and-rejected (synth orbit capture): per-step visible
set is only ~13% of splats (6,198/47,600 on an orbit view), so each splat
gets an optimizer step on ~1/8 of iters → effective per-splat LR ÷8 →
the ~800-iter opacity lift-off never arrives inside the budget (6.45 vs
stock 21.99). Gather/scatter/momentum math verified exact: a forced
all-rows index set reproduces stock (21.98 vs 22.19) through the same
trajectory — `BRUSH_SPARSE_ALL=1` keeps that control runnable, and
`BRUSH_SPARSE_HOST=1`/`BRUSH_SPARSE_DEBUG=1` provide a host-side inds
fallback + count logging for Metal validation. Revisit only if the Adam
step ever dominates Mac step-time — it doesn't (rendering does).

**Prog res is the headline win so far**: +0.65 dB over stock at 27% less wall.
Fewer splats at end (25k vs 38k) — coarse-phase refines see pixel-space
thresholds scaled down, so it densifies less — likely contributing to the
speedup alongside the 16×-smaller early renders.

**Mip filter verdict**: keep `--min-scale-factor 0.1` (stock default). 0
loses −0.18 dB and densifies to 54.8k (floaters cost speed too); 0.3 loses
−0.34 dB. **SSIM weight**: 0.5 > 0.2 > 0 (+0.14 dB over stock) — pulled into
`--quality`. **Seeded LRs**: deep cuts hurt monotonically (0.5/2.0 → −0.81,
0.2/3.0 → −2.04); retune sweep (0.75/1.25, 1.0/1.5) running. **Opac decay**
0.004 vs 0: neutral (−0.03) — keep upstream default. **SH degree**: at
fixed iters, deg1-unlock ≈ stock (−0.05→+0.05); deg3-unlock +0.03 — SH
payoff is view-dependent and nearly free here; the orchestrator's Mac
receipt says SH3-vs-SH1 costs ~2× step time at 290k splats, so on the 60 s
budget SH degree stays a *step-allocation* call — I set unlock fractions,
T2/T7 pick `--sh-degree` by their per-step measurement.

## Quality preset (`--quality`)

Single flag resolving the tuned bundle in `TrainConfig::apply_quality()`:
`res-start-div 4`, `res-full-at 0.5`, `sh-unlock-at 0.15,0.3,0.5` (to deg 3
when `--sh-degree 3`), `ssim-weight 0.5`, `ema-decay 0.95`, seeded
`0.75/1.25`, sparse off (see †). Explicit per-flag values still win when
set — the bundle only fills unset slots. Pending validation vs stock at
equal wall time on `farsight-splat bench`.

## EMA export (`--ema-decay`)

`EmaState` keeps `decay`-weighted copies of `transforms` / `sh_coeffs` /
`raw_opacities` on the inner device, updated after each optimizer step and
re-anchored at refine (row identities change). Eval and export read
`trainer.ema_splats(&splats)` — the raw params keep training. 0 disables.
