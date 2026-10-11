# Mac desktop demo measurements — 2026-10-11

Fresh saved-run trials, Brush v0.3.0 Metal, M5 Pro 48 GB, nice 10. Same 7,500-step
SH3 preset, original 1,454,055-point measured seed strided by five to 290,811
initial points, later growth cap 300k. The full model is published before scoring;
Quest pruning is outside the desktop critical path and was skipped here.

| Drive seconds | Photos | Local rsync s | Prep s | Brush s | Desktop copy s | Post-run file ready s | PSNR dB |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 366.97 | 715 | 2.04 | 4.54 | 202.68 | 0.09 | 209.97 | 19.32 |
| 59.69 | 119 | 0.31 | 0.78 | 212.69 | 0.05 | 213.96 | 25.62 |
| 119.88 | 237 | 0.90 | 1.33 | 177.95 | 0.04 | 180.35 | 21.08 |

These transfers are **local rsync stand-ins**, not tether measurements. USB SSH
at 192.168.55.1 timed out. The 10-second demo idle observation and browser parse
are not included in these local file-ready totals. Shorter trials reuse the full
run's measured seed and score different held-out sets; their PSNR is not a
full-room comparison. The first minute contains only about 1.17 m of motion.

The earlier full preset measured 129.46 s Brush / 135.18 s total at 08:43 EDT,
19.36 dB over 90 views. Fresh full run: 202.68 s Brush, 19.32 dB over 90 views.
Our cargo build and all its rustc children were SIGSTOP from 09:11 EDT, before the
fresh cap runs. Relay, browser, and video workloads remained active; GPU
contention is plausible but was not isolated as the cause. Do not call these
an idle-machine benchmark or promise the older time for the live demo.
The two full-run prepared manifests, seeds, and all consumed image bytes have
identical SHA256 hashes; flags match except paths and both exports contain
300,000 splats. `pmset -g therm` reported no recorded thermal/performance warning,
which does not establish GPU clocks. A walkthrough tab was open during these fresh trials. At 09:31 EDT its current
PLY resource was the 300k cap model; other browser GPU work was not attributed.
With no task GPU training active, AGX reported 39% device/renderer utilization
at 09:32 EDT. A closed-viewer retest is pending; this is evidence of remaining
GPU activity, not proof that one tab caused the timing change.

The complete fresh model is published as `scenes/run-094259-cap-full.ply`.
The matching walkthrough is `/scenes/walk.html?scene=/scenes/run-094259-cap-full.ply&transforms=/scenes/run-094259-cap-full-transforms.json&sh=2`.
The PLY keeps SH3; this walkthrough renders SH2. Browser qualification: background Chrome loaded the fresh 70,801,551-byte PLY
(249.6 ms HTTP resource transfer), `window.__ready=true`, loading status hidden,
and zero page/unhandled-rejection errors. The start view is recognizable but
blurred, with visible floaters; loading is not a visual-quality pass. The task
tab was closed afterward. Real live-run timing is pending rover connectivity. No physical headset
qualification is implied.

Local artifacts: `.cache/autosplat/quality/cap-trials/*-receipt.json` and
`.cache/autosplat/runs/run-094259-cap-*/`; venue photos stay ignored.

## Closed-viewer 60-second retest

At 09:42 EDT the existing compile had finished and no localhost walkthrough tab
remained in the attached Chrome. The same 119-photo, 7,500-step configuration
measured 0.43 s prep, **142.26 s Brush**, 0.037 s publication, **142.83 s file
ready**, 25.57 dB over 15 views. Reusing the separately measured 0.307 s local
rsync stand-in plus the demo's 10 s idle window gives **153.14 s**. This narrowly
misses the 150-second target. The earlier 119-photo trial was 212.69 s Brush;
this retest supports contention as a contributor, while other shared machine
conditions were not controlled. Lower budgets are being measured.

The 5,000-step retest measured **102.28 s Brush**, **103.88 s build/file ready**,
24.62 dB over the same 15 views. With 0.307 s local rsync and 10 s idle this is
**114.19 s post-run**, or about **173.88 s drive + file ready** for the 59.69 s
prefix. Real transfer and browser parsing remain unmeasured. The armed demo
watcher now uses 5,000 steps with the same 300k seed cap / SH3; the demo CLI
preset follows that measured choice. The shared whole-run seed caveat still
applies.

The 4,000-step retest measured **87.48 s Brush**, **89.00 s file ready**, and
24.21 dB over 15 views; with the same stand-in/idle allowance it is **99.31 s**.
The 5k preset trades 14.88 s for 0.41 dB on that prefix and remains under the
150 s goal. These prefix scores do not establish full-room quality.

## Scoring the actual Quest copy

Reloaded the served `scenes/run-20261011T094259.ply` (volume-ranked 300k/SH1,
from the earlier full-seed 5k export) with frozen learning rates, on the exact
Dechante 714-photo camera set. All 300,000 splats and properties survived;
maximum absolute parameter difference was 2.384e-7. The 90-view result was
**11.38 dB** (median 11.15), in 3.95 s for reload/evaluation/export/scoring.
This directly qualifies the compact copy's loss; the unpruned model's training
score must not be presented as that file's quality. `score_model.py` records
all per-property reload differences. The earlier raw 20.40 dB reference used
715 photos; a matched 714-camera frozen raw score is being measured.

The first pinned native importance-seed 2k output scored **19.16 dB / 90** on
714 cameras and exported 1,418,839 splats. Its scorer initially received the
parent of the fork's nested `eval_2000/images/` directory and failed after
training; no durable wall-time receipt survived. The caller now handles both
stock flat and fork nested eval paths and saves trainer timing before scoring.
The rerun is the timing authority. No newer Sai stack is implied by this pinned
34d2d416 build.

Matched frozen reload of the original **2,177,427-splat SH3 full model**, on the
same 714-camera set: **20.59 dB / 90**, median 19.84, 31.93 s reload/eval/export/
score; maximum parameter difference 2.384e-7, no missing properties. The paired
300k SH1 score is therefore an actual export-quality comparison, rather than a
comparison between different held-out sets. Artifacts are private under
`.cache/autosplat/quality/frozen-{raw,volume}-score/`.

The receipted native 2k importance trial measured **129.71 s Brush/eval/export**,
**132.51 s including post-hoc scoring**, **19.15 dB / 90**. It exceeds 120 s before
pipeline preparation, so it does not meet the original combined speed/quality
goal. Its seed was importance-decimated to 484,685 then budget-regrown toward
1,454,055. Later native trials and CPU face extraction share the Mac's live
background workloads; none is an isolated hardware benchmark.

## Face-enabled demo validation

Integrated Dechante's `walk-face-detection` commit 98cc0e2, with CPU inference,
workspace-local model/config paths, an empty-keypoint confidence compatibility
fix, relative sidecar photos, and overlay attachment. Demo extraction runs in
parallel with Brush and publishes its manifest last. Offline Three.js 0.160.0
and Gaussian Splats 0.4.7 modules are vendored with their MIT licenses; no CDN or
font request is needed. Python 3.12 uses ultralytics 8.3.0, open3d 0.19.0,
torch 2.5.1 and torchvision 0.20.1. First cold attempt spent 88.54 s including
library/font/model setup before the empty-result failure; this is not an
extraction-speed result. A fixed warm full-run extraction and combined 60-second
GPU+CPU pipeline are being measured.

The fixed warm full 714-photo extraction completed in **331.55 s CPU** on this
Mac. This does not match Dechante's reported 34 s on another machine. A combined
119-photo pipeline initially bypassed its venv because Python's symlink was
resolved; that failed face stage was 0.11 s and is **not** a face-enabled pass.
The launcher now preserves the venv path, covered by a regression test. Its
room still published in 118.07 s, 24.62 dB / 15; the corrected combined rerun is
the authority for face-enabled latency. The just-started full native 5k trial
was interrupted to prioritize this rerun; it has no completed score.

Corrected combined 119-frame face-enabled 5k trial (10:02 EDT): **112.19 s
Brush**, **61.58 s CPU faces**, **0.00008 s wait after training**, **113.73 s
room + sidecar ready**, **24.61 dB / 15**, one face spot from two sightings.
Adding the separately measured 0.307 s local rsync stand-in and 10 s idle gives
**124.04 s post-run**, or **183.73 s drive + ready** for the 59.69 s prefix.
Preparation/publication are included; browser parsing and real network transfer
are not. The first-minute dataset still reuses the whole saved run's seed.
The watcher has been rearmed at 5k/300k/SH3 with CPU faces enabled.

Browser qualification of that exact face-enabled URL: external HTTPS requests
blocked, local modules loaded, `__ready=true`, loading status hidden, one face
button, zero page/unhandled-rejection errors, and zero HTTPS resource entries.
Selecting the pin opened its boxed camera photo with nonzero natural width,
again with zero errors. Private receipt/screenshot files are in
`.cache/autosplat/quality/faces-*`; the task tab was closed afterward. This is
desktop browser evidence, not a physical Quest test or real network run.

The pinned 34d2d416 Quest blend-weight 500k/SH1 trial completed in **222.17 s**,
**19.61 dB / 90**, with a 600k training cap and the last 200 steps reserved for
fine-tuning after pruning. The exact 500,000-splat SH1 export is published as
`scenes/run-094259-quest-blend-500k.ply`; that timing misses the 120 s goal.
The 300k comparison and actual published-model frozen scores remain pending.
A newer exact Sai benchmark was requested afterward and is a separate revision,
not a replacement interpretation of these older pinned results.

The pinned 300k/SH1 blend-weight trial measured **202.18 s**, **19.48 dB / 90**
in its native evaluation. Frozen reloads of the actual published files scored
**18.99 dB / 90** for 300k (9.74 s evaluation) and **19.19 dB / 90** for 500k
(16.03 s), with maximum parameter differences 2.384e-7 and no property loss.
These lower exported-model scores are the publication quality authority.
The 300k model is `scenes/run-094259-best.ply`; it respects the requested Quest
count and loses 0.20 dB against the 500k export while substantially improving on
the volume-pruned 11.38 dB copy. Matched first and midpoint camera renders were
inspected in the private `matched-export-views.png`: blend pruning retains the
rover/shoe outline and room boundaries that volume pruning obscured, while
fine texture remains blurred. No venue images are posted publicly.

The exact 300k/SH1 candidate loaded in the locally served `/splat.html`: 300,000
splats, reported load 265 ms, `error=null`, and zero page/unhandled-rejection
errors. This was a desktop, non-XR browser sample; its instantaneous FPS is not
a headset frame-budget qualification. The task tab was closed.
