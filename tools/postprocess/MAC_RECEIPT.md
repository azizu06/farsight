# M5 Pro saved-run receipt — 2026-10-11

Real saved rover data: `~/farsight-captures/run-20261011T094259/`, 715 posed
photos over 366.97 seconds, 55.80 m of camera travel, and a 1,454,055-point
measured LiDAR seed. Hardware: Apple M5 Pro, 48 GB. Brush v0.3.0 Metal runs
at nice 10; live desktop/relay services remain running.

## Full run and previous cut

The original 12-second motion-pause threshold split this capture unnecessarily:
photos continue with a maximum 1.33-second interval, while parked pauses reach
54.85 seconds. Automation now uses a configurable 60-second motion-pause
threshold and retains the entire 715-frame run when it contains one drive.
The standalone `find_drives.py` retains its 12-second default.

All 715 JPEGs pass strict decoding. The 29 valid files shorter than Brush's
16,387-byte initial read are preserved as padded lossless PNGs; original JPEGs
remain the scoring ground truth. The previous implementation excluded them.

All timing below is wall time with local input: **pull = 0**, not a real rsync
measurement. Held-out scores are Brush's final training evaluation, before
Quest pruning. The cut and full run have different held-out photo sets, so
these scores do not establish an apples-to-apples quality improvement.

| Dataset / training preset | Steps | Prep | Train | Prune | Publish | Score | Total | Held-out mean |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Previous 154-frame / 79.20 s cut, SH1, 290,811 initial splats | 5,000 | 4.69 | 117.00 | 0.43 | 0.01 | 1.89 | 124.03 | 20.46 dB / 20 views |
| Whole 715-frame run, SH1, 290,811 initial splats | 5,000 | 1.62 | 64.96 | 0.14 | 0.02 | 1.10 | 67.90 | 18.92 dB / 90 views |
| Whole run, Dechante SH3/full 1.45M seed | 5,000 | 2.06 | 338.10 | 0.58 | 0.02 | 2.33 | 343.21 | 20.40 dB / 90 views |
| Whole run, SH3, 290,811 initial splats | 7,500 | 3.11 | 129.46 | 0.48 | 0.02 | 1.96 | 135.18 | 19.36 dB / 90 views |

The exact reference settings reproduce Dechante's 20.4 dB score, but exceed
the Mac render budget. Brush exported 2,177,427 splats despite the 300k growth
limit. Per Firstmate's follow-up, the default remains this exact 5k full-seed
method. `--fast` selects 2k steps, predicted ~135 s training from this trial;
that prediction is not an independently timed run. The 7.5k seed-capped
alternative meets approximately two minutes of training, at lower PSNR, and
is retained as `dist/scenes/run-20261011T094259-mac-cap.ply`. The full-seed
result is retained as `dist/scenes/run-20261011T094259-fullseed.ply`.

The previous cut remains locally available as
`dist/scenes/run-20261011T094259-cut.ply` for comparison. The full-run cheap
preset covers the room but still shows visible streaks/fog in desktop orbit.

Reference refresh: **Dechante's** `origin/splat-fast-steps` at
[217bb22](https://github.com/azizu06/farsight/commit/217bb22). New step-budget,
comparison-grid, detached Windows launcher, camera/GLB renderer and offline
face benchmark files are imported without running the face benchmarks. His
`walk-splat-viewer` at 420696c has the same `walk.html` already imported here.
The reference's 1600-pixel limit does not resize this run's 640×480 photos.

## Upright and start pose

The generated URL includes `sh=1&flip=1&pos=...&yaw=...`. The midpoint recorded
normal-tracking camera is transformed with the same X-axis flip as the PLY.
Its position minus configurable eye height (default 1.6 m) supplies the viewer's
XR floor point; camera forward supplies yaw. This uses the viewer's existing
parameters and changes no renderer/headset code.

**Desktop always invokes `frameDesktop()` and starts in orbit**, even with
`pos`/`yaw`. The inside-room start applies to the XR rig; that behavior has
source/unit evidence, not physical Quest acceptance. Camera pitch is not a
supported start parameter. The upright URL must be used; opening the bare PLY
URL does not persist orientation or pose in the viewer.

## Checks and limits

Nine focused unit tests pass, including strict decoding, preserving small
JPEGs, full-run pause grouping, native dense.ply seed alias, matched pose/model
flip, watcher backlog filtering, capture-idle transfer ordering, bandwidth
arguments, serial once-only processing and atomic publication.

The preceding delivery's `npm --prefix quest run test:judge` ran twice: 12/15
while training, 11/15 with Brush stopped. Detections, held-deadman drive,
Explore and scan failed with stale-camera/lease/input interruptions. No Quest,
relay, demo or Rust files changed here; this follow-up does not claim that
simulator smoke is green or that physical driving/headset performance passed.

Jetson SSH metadata is now reachable. At 12:36 UTC, 14 archives were observed,
five with a measured seed. The newest capture increased from 10 to 562 images
while the worker was testing; no archive/image/point-cloud transfer occurred.
Native completed phone archives contain `dense.ply`, now accepted as `init.ply`
in the prepared dataset. Pulls remain deferred until all observed captures are
unchanged for at least 60 seconds. Camera capture must end after the drive;
this is the requested image-idle heuristic, not a motor-state signal.

No photos, PLYs or screenshots are uploaded to GitHub. Desktop page access,
held-out reconstruction scores and watcher startup do not prove a physical
Quest walkthrough or next-drive automatic completion.

The final default scene is the exact full-seed 5k result, pruned to 300k/SH1.
Full local receipts: `.cache/autosplat/runs/run-20261011T094259/20261011T083520/`
(reference 5k) and `20261011T084113/` (Mac seed-capped 7.5k). A separate real
`--total-steps 1` load test verifies all prepared photos; future automatic runs
include this stage and record `load_test_s` separately.

## Final publication and armed watcher

At 12:44 UTC the full-seed 5k Quest copy returned HTTP 200 and matched the
published SHA-256 `799c2f783acb39ed9d647951c56a4e7b4e88175d058c65896ab9c833e5c98ad3`.
It is 27,600,652 bytes / 300,000 splats / SH1. Desktop Chrome loaded it in
476 ms with `__splatStats.error: null` and zero captured window errors or
unhandled rejections. Sampled desktop 115 fps is not headset performance.
The upright orbit screenshot still shows severe distant streaks/fog; matching
training PSNR does not establish good quality after 2.18M→300k pruning.

Viewer URL:
`http://localhost:8080/splat.html?scene=scenes%2Frun-20261011T094259.ply&type=ply&sh=1&flip=1&pos=-0.20223%2C-1.23749%2C3.97135&yaw=-171.320`.

The watcher was **armed at 12:44:33 UTC**, PID 74043, detached from this task's
terminal. It uses the exact default 5k / SH3 / full seed, `--since
20261011T122834`, and the shared `.cache/autosplat` lock. Its first real SSH
poll saw the ongoing 12:31 capture at 1,515 images, `idle_ready=0`, and made no
pull. The next rehearsal/current active capture will qualify only after it
ends and all captures stay unchanged for 60 seconds. Full remote transfer and
next-drive completion remain unproven while the capture is active.

Local operational receipts: `.cache/autosplat/watcher-armed.json`,
`watcher.pid`, `watcher.log`. **Preserve this worktree while the watcher runs**;
Firstmate should move/rearm it from the integrated stable checkout before
recycling the worktree. Stop only this task's PID using the README command.

Dechante's [094259 fast release](https://github.com/azizu06/farsight/releases/tag/splat-094259-fast)
appeared during final verification. Its 2k/5k PLYs are downloaded through
`gh-axi`, pruned locally with `tools.splat.prune` to 300k/SH1, and published
atomically as `scenes/dechante-094259-2k.ply` and
`scenes/dechante-094259-5k.ply` for side-by-side comparison.
The Mac's reproduced 5k result remains `scenes/run-20261011T094259.ply`.

Release asset download completed successfully. The 2k source has 1,739,510
splats (410,525,912 bytes); the 5k source has 2,159,401 (509,620,188 bytes).
Both published Quest copies have 300,000 splats / SH1.

## Face-enabled 60-second demo — 2026-10-11 10:02 EDT

Dechante's face pins (`walk-face-detection` 98cc0e2) now run on CPU alongside
Brush, with local viewer modules and per-run face sidecars. On the saved 094259
prefix: 59.69 s, 119 photos, original measured seed strided to 290,811 points,
5,000 steps, SH3, later growth cap 300k, nice 10:

| Stage | Seconds |
|---|---:|
| Prep | 1.33 |
| Brush including final eval/export | 112.19 |
| CPU faces, overlapping Brush | 61.58 |
| Face wait after Brush | 0.00008 |
| Room + face sidecar ready | 113.73 |
| Separately measured local rsync stand-in | 0.31 |
| Demo idle observation allowance | 10.00 |
| Post-run ready with those allowances | **124.04** |

Held-out PSNR: **24.61 dB / 15 views**; one face spot from two sightings.
This prefix reuses the whole saved run's seed and is not a full-room quality
comparison. Real Jetson transfer and browser parsing are excluded from 124.04 s.
The Jetson's USB and Tailscale SSH paths were unavailable for a real live-run test.

The exact face-enabled walkthrough loaded with HTTPS blocked: local modules,
zero external resource entries, zero page/unhandled-rejection errors. Selecting
the face pin opened its boxed camera photo successfully. The task tab was closed.
The armed Mac watcher uses this 5k/300k/SH3 + CPU faces preset, newest run only,
10 s stable capture metadata, and unlimited rsync in explicit `--demo` mode.
Non-demo mode retains 60 s stability and bandwidth limiting.

The separate `scenes/run-094259-best.ply` Quest candidate is 300k/SH1, blend-weight
pruned and fine-tuned using Sai's pinned native trainer. Its **actual exported
PLY** scores **18.99 dB / 90** on the Dechante 714-photo set (frozen reload, maximum
parameter delta 2.384e-7), versus 11.38 dB for the earlier volume-pruned copy.
It loads in local `/splat.html` with exactly 300,000 splats and zero page errors.
This is desktop software evidence; physical Quest and real drive-end timing
remain unqualified. See [QUALITY_RECEIPT.md](QUALITY_RECEIPT.md) for full-run,
short-run, native, and export-quality measurements and their limits.
