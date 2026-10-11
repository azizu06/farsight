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

## Neural Engine path (macOS, `vision` feature)

`farsight-views` gains a macOS-only engine for the two quality-affecting
stages, gated `#[cfg(all(target_os="macos", feature="vision"))]` +
`--features vision` cargo flag; Linux compiles and runs identically with
the feature off.

- `masks --engine auto|temporal|vision`: `vision` runs
  `VNGeneratePersonSegmentationRequest` (qualityLevel =
  `VNQualityBalanced`, outputPixelFormat `L008`) via objc2 msg_send —
  Apple's balanced tier is the ANE-scheduled path. Person pixels become
  transient (mask byte 0); layout, `mask_path` patching, and the
  AlphaMode::Masked consumption path are identical to `temporal`
  (verified: `--engine temporal` rewrote the same 24/24 masks on
  Linux). Run it on a worker thread and it overlaps GPU training.
- `select --engine auto|rust|vision`: `vision` replaces the Rust
  Laplacian-variance blur score with `vImageConvolve_Planar8`
  (Accelerate) on the same decoded luma plane — same metric, SIMD path.
- `auto` resolves to vision on macOS+feature, temporal/rust elsewhere;
  explicit `vision` on Linux exits with a clear error.

**Mac verification checklist (bench_mac.sh / manual, all required before
claiming ANE behaviour):** `cargo build --release --features vision` in
`splat/farsight-views`; `farsight-views masks --engine vision --in-dir
CAP` — assert `masks/images/*.png` written and `mask_path` patched for
every frame; visually spot-check one mask (person pixels 0); time it vs
`--engine temporal`; run `select --engine vision` vs `rust` and compare
score ordering + wall time; confirm ANE use with Instruments (ANE
Power/Usage template) — do NOT claim ANE scheduling without that trace.
No ANE or Metal speed claims are made from Linux runs.

### masks — transient/people masking

For each train view: decode the k pose-nearest neighbours at 320 px, take the
per-pixel temporal median, mark `|I − median| > thresh` on any channel as
transient, dilate, write `masks/images/<stem>.png` and patch
`mask_path` into `transforms.json`. Brush's existing `AlphaMode::Masked` loss
path (`find_mask_path` + alpha-weighted loss kernels) consumes them — **zero
kernel churn**: people ghosts simply stop contributing to the loss.

### exposure — per-view affine appearance compensation

`--mode` picks **exactly one** appearance strategy (they must not
double-apply); the choice is recorded in `views_meta.json` in the output dir
for bench scripts:

- **`offline`** (default): per-channel affine `p → g·p + b` fitted so each
  train view's mean/std matches the dataset-median mean/std, baked into
  rewritten train JPEGs. Eval untouched; `--eval-half-fit` fits each eval
  image's affine on its top half only for the appearance-fair metric.
- **`learned`**: frames passed through unchanged + `views_meta.json
  {"appearance":"learned","appearance_lr":0.005}` — use with
  `brush-cli --appearance-lr` (see below).
- **`none`**: passthrough, no compensation.

## Learned per-view affine (brush-train/appearance.rs, T6)

`--appearance-lr 5e-3` enables a per-view `rgb' = rgb @ gain + bias`
(3x3 + 3 = 12 params/view) applied to the **render** before `image_loss`.
Params are autodiff leaves — their grads ride the shared
`loss.backward()` and are stepped by a dedicated AdamScaled at
`--appearance-lr` (low LR, default 0 = off). `--appearance-warmup-frac
0.05` keeps the bank at identity for the first 5% of the schedule so
geometry locks first; `--appearance-reg 0.001` adds
`reg·(|gain−I|²+|bias|²)` to each view loss.

Fair eval: with `--appearance-lr > 0`, `run_eval` also reports
`PSNR (appearance-fair)` — per-channel affine LSQ-fitted on the **left**
half of each eval frame, applied to and scored on the **right** half —
alongside raw PSNR (`appearance::eval_appearance_psnr`).

Verified on lavapipe (`/tmp/cap_sel`, 60 iters, `--appearance-lr 5e-3`):
trained end-to-end with no grad/state errors, eval printed
`PSNR (appearance-fair) 23.620` next to raw `PSNR 14.9956`.

## Small-JPEG drop (stock Brush vs this fork)

Stock Brush v0.3.0's loader demanded a ~16,387-byte initial read per image —
the 29 real-capture JPEGs under that size were silently dropped, and
Dechante's `after_run.py` worked around it by transcoding them to padded
PNGs. Our vendored loader does NOT have the drop: `LoadImage::dimensions`
goes through `brush_vfs::read_until_parsed` (chunked, EOF-tolerant) and
`decode_jpeg_scaled` reads the full buffer. Verified: a 6-view capture of
5,426-byte JPEGs loads all views (`5 training, 1 eval` — no drops). No
padding hacks needed; the padded-PNG workaround is unnecessary for this
pipeline. Side find: `--eval-split-every 0` panics in nerfstudio.rs:351
(divide-by-zero) — use eval-split >= 1.

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

## Ablation matrix

Verified end-to-end 2026-10-11 on lavapipe: `brush-cli /tmp/cap_sel
--total-train-iters 100 --eval-split-every 8` → loaded 24 train / 5 eval
views + 24 `mask_path` masks (VFS 55 files), trained 100 iters in 63 s,
`Eval iter 100: PSNR 15.4558` (procedural test scene, random-splat init,
100 iters — correctness run, not a quality datapoint). Loader GPU-resident
path executed: all 24 packed images ≈ 29 MB ≪ 6 GiB cache → zero per-step
upload after first epoch. PSNR-neutral by construction (same pixels).

| change | capture | verified | PSNR vs base | notes |
|--------|---------|----------|--------------|-------|
| loader: eager prefetch + img_gpu | any | ✓ compile+train (f6d2287) | neutral | removes cold-start duplicate decodes + per-step upload |
| select 40→24 | /tmp/cap_sel | ✓ | pending synth | culled blurry f0025 + 2 parked dupes; eval {0,8,16,24,32} |
| +masks | /tmp/cap_sel | ✓ loaded | pending synth | people-blob frames get mask_path, loss-weighted out |
| +exposure | /tmp/cap_exp | ✓ wrote | pending synth | eval-half-fit variant available |

Pending: rerun on T1's `farsight-splat synth` capture + `bench_mac.sh`
for real PSNR-vs-seconds numbers once the harness lands.

## Design notes / not-yet-landed

- **`pose-refine` subcommand**: implemented in farsight-views (Gauss-Newton
  photometric SE3 refinement against LiDAR anchors — ply rgb, else
  LiDAR-depth-projected neighbour pixels; Huber + per-iter step clamp +
  cost-decrease guard). On a checkerboard synth with periodic texture it
  over-corrects (photometric ambiguity), which is a property of that
  test scene rather than the method; needs a non-periodic synth or the
  real init.ply to score. Not wired into the pipeline yet.
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
