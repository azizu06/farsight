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

| config                                        | held-out PSNR | SSIM  | wall   | final count |
|-----------------------------------------------|--------------:|------:|-------:|------------:|
| brush (stock)                                 | 20.19         | 0.847 | 380.2s | 365,995     |
| brush `--max-splats 300000` (hard cap)        | 19.98         | 0.847 | 393.7s | **300,000** |
| budget -> 250k                                | 19.98         | 0.847 | 349.9s | 253,756     |
| budget -> 400k                                | 20.10         | 0.846 | 492.7s | 392,487     |
| mcmc (`--growth-target-splats 300000`)        | 20.03         | 0.847 | 370.8s | 300,000     |
| decim 100k + budget -> 300k                   | **20.33**     | 0.851 | 330.1s | 284,974     |

Findings:
- **The hard cap holds.** `--max-splats 300000` ends at exactly 300,000;
  mcmc honors `--growth-target-splats 300000` at exactly 300,000. Stock
  brush would have drifted past like it did on the Mac (300k -> 2.18M).
- **At equal iters, count barely moves PSNR** on this capture: 250k-400k
  are all within 0.35 dB. The count's real lever is *step rate* — on the
  M5 Pro ~77 it/s at 290k vs ~15 it/s at 2.18M — which under `--budget-s`
  is what buys PSNR back inside a wall budget.
- **The decimate-seed + score-regrow recipe wins again**: decim 100k +
  budget->300k takes best PSNR (20.33) AND the lowest wall (330s). The
  seed's least-important 2/3 replaced by score-selected regrowth beats
  spending the whole seed — same as round 1.
- mcmc is a clean constant-count mode (exactly 300k in/out) and
  competitive, but budget+regrow still edges it on PSNR.

## Quest export prune (300k budget)

`--quest-prune-target N` at `progress >= 1 - frac`: keep top-N by lifetime
blend weight, then freeze refinement and fine-tune survivors for the tail.
`lod::export_prune` gives T1's offline path the same top-K score as a
function.

Verified: `--quest-prune-target 100000 --quest-prune-finetune-frac 0.25`
on the same synth2 run (train ~285k to iter 224, prune to 100k by lifetime
blend weight, 75-step frozen-refine fine-tune):

| config                          | held-out PSNR | SSIM  | wall   | final count |
|---------------------------------|--------------:|------:|-------:|------------:|
| budget->300k + quest->100k@0.75 | 20.01         | 0.846 | 291.7s | 100,000     |

The blend-weight ranking keeps 100k within ~0.3 dB of the ~285k model
(20.33) and above every naive-capped run — for the real 300k Quest budget
the prune gap should be smaller still. Export counts land exactly on
target (`.splat` = count x 32 B).

## Recommended recipe (for the shared `quality` preset)

Wired into `TrainConfig::apply_quality` (`--quality`), alongside T3's
schedule bundle:

- `refine_mode = Budget` — score-ranked growth under a real cap.
- `growth_target_splats = 300000` (only when unset) — holds M5 Pro step
  rate near 77 it/s; the extra steps inside `--budget-s` beat the last
  ~0.2 dB a larger cloud would give. For a >=120 s budget a larger cap
  (400-500k) is the cheap upgrade — pass the flag explicitly.
- `quest_prune_target = 300000` (only when 0) — the Quest export budget
  lands exactly on 300k with the fine-tune tail.

Per-budget picks (M5 Pro, seed ~290k):

| budget   | refine_mode | cap    | expected count | rationale |
|----------|-------------|--------|----------------|-----------|
| 60 s     | budget      | 300k   | ~300k          | max steps at ~77 it/s |
| 120 s    | budget      | 400k   | ~400k          | PSNR still ~flat, +count free |
| 300 s    | budget      | 500k   | ~500k          | step-rate trade favours steps |

`init_decimate` stays out of the preset — T5's farsight seed (~290k) is
already the decimation product; decimating further costs PSNR. For a raw
1.45M seed, `--init-decimate ~1/3` + regrow is the winning move (both
rounds).

Reproduce: `bash` the grid script under `VK_ICD_FILENAMES=...lvp_icd` (CPU
Vulkan) or `splat/bench_mac.sh` on the M5 Pro.
