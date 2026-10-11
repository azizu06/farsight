# M5 Pro saved-run receipt — 2026-10-11

Hardware: MacBook Pro, Apple M5 Pro, 48 GB (verified with system_profiler).
Brush v0.3.0, Metal, `nice -n 10`, 640 px, SH1, 300k growth cap. This is real
saved rover data on the Mac, with no provider calls or synthetic training images.

Input: `~/farsight-captures/run-20261011T094259/`, 715 posed photos plus a
1,454,055-point measured LiDAR seed. Strict decoding/header preparation excluded
29 JPEGs shorter than Brush's 16,387-byte fixed read. Dechante's `find_drives.py`
selected the longest continuous segment: 154 frames, 79.20 s (frames 289–442 in
the filtered set). The seed was subsampled every fifth point (290,811 initial
splats), then trained to 300,000 splats. Every eighth selected image was held
out: **20 views**, a different dataset from Dechante's full-run 90-view scores.

## Timing

Seconds are wall time, including load/eval/export in the Brush stage. All runs
used local data; pull time is **0**, not a live rsync measurement.

| Steps | Prep | Brush | Prune | Publish | Score | Total | Held-out mean PSNR |
|---|---:|---:|---:|---:|---:|---:|---:|
| 500 | 3.18 | 14.31 | 0.23 | 0.01 | included in total | 18.47 | 17.89 dB |
| 4,200 | 2.40 | 129.85 | 0.74 | 0.02 | included in total | 135.76 | 20.28 dB |
| 3,800 | 5.87 | 89.45 | 0.35 | 0.03 | 0.92 | 96.64 | 20.16 dB |
| 5,000 | 4.69 | 117.00 | 0.43 | 0.01 | 1.89 | 124.03 | 20.46 dB |

The 4,200-step trial overlapped a judge-flow smoke run. The 3,800/5,000-step
trials ran without that smoke workload. Background desktop/relay services
remained running; this is not a physical VR frame-budget qualification.

**Default: 5,000 steps**, measured at 42.74 steps/s including Brush startup,
evaluation and export: **117.00 s training, 124.03 s total**, 20.46 dB mean
held-out PSNR (median 20.55, min 17.47, max 25.07). The remote path additionally
waits at least 60 s to observe capture completion and spends time transferring
the archive. It does not promise two minutes from the last motor command.

Reproduce:

```sh
.venv/bin/python tools/postprocess/after_run.py \
  --local ~/farsight-captures/run-20261011T094259
```

The complete JSON receipt is retained locally at:
`.cache/autosplat/runs/run-20261011T094259/20261011T082118/timing-receipt.json`.

## Publication and viewer

The final PLY is 27,600,652 bytes, 300,000 splats, SH1, atomically installed at:
`/Users/aziz.u/.treehouse/farsight-5a56e1/5/farsight/.cache/050-runtime/quest/dist/scenes/run-20261011T094259.ply`.

GET `/splat.html` and GET `/scenes/run-20261011T094259.ply` on the actual live
relay `http://127.0.0.1:8080` both return **200**. The served PLY is byte-identical
to the published file. The existing viewer loads the scene with 300,000 splats,
`__splatStats.error: null`, and zero captured window-error/unhandled-rejection
page events. The final 5,000-step export loaded in **225 ms**; the sampled desktop
frame rate was 77 fps (`xr: false`), not a headset performance measurement.

URL: `http://localhost:8080/splat.html?scene=scenes/run-20261011T094259.ply&type=ply`.
Use the relay's headset-reachable origin on Quest. This test used desktop Chrome,
not a physical headset. Chrome's primary controller could not attach to the
existing browser; the maintained Browser Harness fallback performed the check.

## Checks and limits

- Six focused tests pass: capture-idle transition (including incomplete active
  runs), 60-second transfer wait/bwlimit, offline saved runs, strict JPEG decode
  and seeded drive prep, once-only serial watcher processing, atomic publish and
  exclusive process ownership. SIGTERM interrupts/reaps an active subprocess
  before the runner releases its lock.
- All 14 imported reference files are byte-identical to Dechante's branch.
- `npm --prefix quest run test:judge` was run twice: **12/15** during the first
  Brush trial, **11/15** with Brush stopped. The second run failed detections,
  held-deadman drive, Explore and scan on stale-camera/lease/input interruptions.
  Quest, demo, relay and Rust files are unchanged in this branch. The simulator
  smoke is not green; no live-driving smoothness or physical readiness claim.
- Jetson Tailscale SSH timed out. Real remote inventory/rsync remains unverified;
  the transfer ordering and bandwidth arguments have fixture/mock evidence.
- The watcher is implemented and its startup command is documented in
  [AFTER_RUN.md](AFTER_RUN.md); it has not been left running from this disposable
  worktree. Start it from the integrated stable checkout.
- The selected drive supports a partial room; unseen surfaces and moving people
  remain reconstruction limitations. No complete-room or perfect people claim.

No photos, PLYs or screenshots were uploaded to GitHub.
