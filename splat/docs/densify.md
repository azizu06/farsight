# splat-turbo densify notes (T4: refinement, pruning, splat-count budget)

Scope: `splat/brush/crates/brush-train/src/{train,stats,splat_init,lod,multinomial,config}.rs`
and the refine gate in `splat/brush/crates/brush-process/src/train_stream.rs`.
T3 owns the LR schedule in the same `train.rs`; the shared seam is
`trainer.progress()` (elapsed/budget under `--budget-s`, else iter/total), so
every window below is a schedule fraction and means the same on any machine.

Step cost scales with splat count x screen footprint: on the M5 Pro run
094259 (715 views, 1.45M seed) stock refine grew 300k -> 2,177,427 splats at
~15 steps/s, while a 290k cloud runs ~77 steps/s. The count is the single
biggest speed dial, and where the splats sit decides the PSNR you buy back.

## What landed (commits on `splat-turbo`)

- `6af1160` — refine modes (`--refine-mode brush|budget|mcmc`), budgeted
  score-based growth (`--growth-target-splats`), Speedy-Splat contribution
  pruning (`--prune-contrib-*`), importance seed decimation
  (`--init-decimate`), `lod::export_prune`.
- `5beedab` — Quest export prune: run-lifetime blend weight
  (`contrib_lifetime` = per-view sum of `visible * opacity * radius^2`,
  accumulated every step, kept aligned through prune/relocate/split) +
  `--quest-prune-target` fired at `progress >= 1 - quest_prune_finetune_frac`
  with refinement frozen after, so the tail fine-tunes the survivors.
- this — HARD cap for every mode: `growth_target_splats` (else `max_splats`)
  now bounds resample, oversized splits and score growth, and any over-cap
  count is pruned at refine entry. Stock `brush` could leak past it; that
  path is now closed.

Contribution score (both pruning and the export prune):
`contrib_i = sum_steps( visible_i * opacity_i * max_screen_radius_i^2 )`,
i.e. accumulated blend weight across all train views — zero extra kernel
cost, read off the forward aux tensors.

## Round 1 — refine modes + seed decimation (lavapipe CPU)

T5's `farsight-seed-synth` capture: 48 views 320x240, 150,500-dot seed,
200 iters, refine-every 50, eval-every 200, 6 held-out views. Wall numbers
are CPU-relative; the ranking and the count column are what transfer.

| config                                        | held-out PSNR | SSIM  | wall   | final count |
|-----------------------------------------------|--------------:|------:|-------:|------------:|
| brush (stock)                                 | 20.93         | 0.775 | 123.6s | 206,708     |
| budget -> 300k                                | 21.14         | 0.791 | 148.3s | 280,220     |
| mcmc (constant count)                         | 20.80         | 0.777 | 118.9s | 150,573     |
| brush + contrib-prune 15%                     | 20.91         | 0.772 | 107.7s | 161,049     |
| init-decimate 75k                             | 21.00         | 0.779 | 75.9s  | 114,508     |
| decim 75k + budget -> 150k                    | 21.22         | 0.792 | 90.9s  | 140,079     |
| decim 75k + budget -> 150k + contrib 10%      | 21.24         | 0.792 | 100.6s | 140,080     |
| decim 50k + budget -> 150k                    | 21.40         | 0.788 | 87.4s  | 136,538     |
| decim 25k + budget -> 150k                    | 21.18         | 0.765 | 66.4s  | 77,053      |
| decim 50k + brush (uncapped regrow)           | 21.07         | 0.772 | 72.9s  | 78,166      |
| decim 50k + budget -> 300k                    | **21.47**     | 0.790 | 94.1s  | 141,742     |

Findings:
- **Importance-decimate the seed first, then regrow by score.** The 150k
  seed decimated to ~1/3 + budget-regrow beats both pure decimation and
  stock growth on PSNR *and* wall: +0.54 dB at -24% wall vs brush base.
  Score-selected regrowth is itself an importance selection.
- Budget mode > brush at equal iters (+0.2 dB) because every added splat is
  score-ranked; mcmc trails slightly on this small scene (relocation noise
  with too few donors), but keeps the count flat by construction.
- More regrowth headroom helps: `-> 300k` beats `-> 150k` at equal iters.
  The cap buys quality, it doesn't just save memory — up to the wall-clock
  limit the count sets.
- Contrib-prune trades ~0 for speed inside this window; its value is on the
  dense-seed runs where it removes the tail the budget ramp keeps.

## Round 2 — hard-cap sweep on T1's realistic synth

`farsight-splat synth` capture: 96 views 640x480, 300,000-dot seed (ARKit
pose noise, per-frame auto-exposure, holey LiDAR), 300 iters, refine-every
60, eval-every 300, 12 held-out views.

<!-- GRID2 -->

## Quest export prune (300k budget)

`--quest-prune-target N` at `progress >= 1 - frac`: keep top-N by lifetime
blend weight, then freeze refinement and fine-tune survivors for the tail.
`lod::export_prune` gives T1's offline path the same top-K score as a
function.

<!-- QUEST -->

## Recommended recipe (for the shared `quality` preset)

<!-- PRESET -->

Reproduce: `bash` the grid script under `VK_ICD_FILENAMES=...lvp_icd` (CPU
Vulkan) or `splat/bench_mac.sh` on the M5 Pro.
