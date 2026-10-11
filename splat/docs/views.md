# farsight-views — capture conditioning + fast loading (T6)

Phone captures have three quality hazards the baseline ignores: auto-exposure
variation frame to frame, ARKit pose drift, and transient content (people
walking past a parked rover). This doc covers the `farsight-views` CLI and the
`brush-dataset` loader rework that condition the capture before/at train time.

All numbers below are lavapipe (CPU Vulkan) relative numbers — the VM has no
GPU; absolute speed only counts on the Mac via `splat/bench_mac.sh`.

## CLI (splat/farsight-views)

```
farsight-views select   --in-dir CAP --out-dir OUT --budget N \
    --eval-every 8 --blur-drop-frac 0.05 --blur-rel-thresh 0.5 \
    --dup-eps-t 0.02 --dup-eps-r 0.01 --dir-weight 0.5
farsight-views masks    --in-dir OUT [--transforms OUT/transforms.json] \
    --k 5 --res 320 --thresh 28 --dilate 2
farsight-views exposure --in-dir OUT --out-dir EXP [--eval-half-fit] \
    --jpeg-quality 95
```

Pipeline order: `select` → `masks` → `exposure` → `brush-cli EXP`.

### select — view selection for a budget

1. **Blur cull** — variance of the Laplacian on a 160 px decode (jpeg IDCT
   1/4-scale, ~no decode cost). Drops the worst `--blur-drop-frac` of train
   views and any view below `--blur-rel-thresh × median`.
2. **Parked-duplicate cull** — greedy pose clusters: translation < `--dup-eps-t`
   AND look direction < `--dup-eps-r` collapse; sharpest frame wins. This is
   what removes rover-parked stretches that contribute nothing but ghost
   disagreement.
3. **Coverage-max subset** — farthest-point sampling over
   `[pos, dir_weight·forward]` down to `--budget` train views, so a step-budget
   run still sees the whole room, not a rediscovered subset.
4. **Held-out split** — frames with `idx % eval-every == 0` go to
   `transforms_val.json` *before* culling, identical membership to Brush's
   `eval-split-every` on the unprocessed capture, so ablations compare to the
   same eval set as the 094259 baseline.

Emits a self-contained capture: `transforms.json` (train), `transforms_val.json`
(eval), `images/` (hardlinks).

### masks — transient/people masking

For each train view: decode the k pose-nearest neighbours at 320 px, take the
per-pixel temporal median, mark `|I − median| > thresh` on any channel as
transient, dilate, write `masks/images/<stem>.png` and patch
`mask_path` into `transforms.json`. Brush's existing `AlphaMode::Masked` loss
path (`find_mask_path` + alpha-weighted loss kernels) consumes them — **zero
kernel churn**: people ghosts simply stop contributing to the loss.

### exposure — per-view affine appearance compensation

Per-channel affine `p → g·p + b` fitted so each train view's mean/std matches
the dataset-median mean/std (per-channel, ghost-robust at 640 px). Applied to
the **train images only**; eval is untouched by default so PSNR is measured
against real camera output. `--eval-half-fit` fits each eval image's affine on
its top half only, then applies it to the whole image — the honest
"appearance-fair" variant for the +1–2 dB regime on auto-exposure captures.

## brush-dataset loader rework (commits f6d2287…)

- **Eager disjoint prefetch**: view indices are partitioned round-robin across
  loader tasks, so every view is decoded + packed exactly once at cold start —
  before this, tasks shuffled independently and early steps re-decoded the
  same views (16 tasks × overlapping random streams).
- **GPU-resident images**: `SceneLoader::with_device` uploads the packed
  `[H,W]` u32 once at decode time; `SceneBatch.img_gpu` hands the trainer a
  resident `Tensor<2,Int>` — zero per-step decode and zero host→device copy
  after the first epoch. `None` falls back to the old per-step upload (wasm /
  oversized datasets).
- **`view_index`** on `SceneBatch` addresses per-view parameters
  (appearance affine, pose deltas) — plumbing for the learned-affine hook
  below.

## Ablation matrix (fill as runs land)

| change | capture | steps | s/it (lava) | PSNR | notes |
|--------|---------|-------|-------------|------|-------|
| baseline | /tmp/cap 40f | | | | |
| select 24 | /tmp/cap_sel | | | | 1 blurry + 2 parked culled |
| +masks | /tmp/cap_sel | | | | |
| +exposure | /tmp/cap_exp | | | | eval-half-fit |

## Design notes / not-yet-landed

- **Learned per-view affine** (nerf-w style, 6 params/view applied to the
  *render* before the loss): `view_index` is plumbed through `SceneBatch`;
  remaining work is a per-view param bank in `SplatTrainer` (hook point:
  `train.rs` `step()` right before `image_loss`). Needs an Adam slot in the
  shared param table — coordinated with the brush-train owner before touching
  the optimizer. Preprocessing-side normalization (above) captures most of
  the auto-exposure win today.
- **Per-view SE3 pose deltas**: Brush has **no** camera-pose gradients
  (`burn_glue.rs` produces only per-splat grads). Options: (a) 2.5D direct
  photometric alignment — median splat depth per view + GT gradients →
  Gauss-Newton update of each view's camera, fully implementable inside
  brush-dataset preprocessing; (b) differentiable camera params in the
  projection kernel — cross-lane, needs coordination. Chosen path: (a) as a
  `farsight-views pose-refine` subcommand that rewrites `transform_matrix`.
- **Resolution levels**: `with_image_scale(cumulative_scale)` rebuilds LOD
  views; the eager prefetch covers each rebuilt scene identically. Levels to
  be agreed with T3 (they own renderer-side resolution knobs).
