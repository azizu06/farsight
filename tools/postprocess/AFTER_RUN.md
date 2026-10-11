# Automatic Mac rooms after a drive

This runner builds on **Dechante's** `splat-fast-steps` tools, refreshed
from [commit 217bb22](https://github.com/azizu06/farsight/commit/217bb22) (2026-10-11). [README.md](README.md) retains the original
workflow and RTX 4070 results. Mac automation lives in `after_run.py`; it does
not change the Jetson, renderer, relay, or headset app.

## Judging desktop demo

The explicit demo mode uses the newest run only, **10 seconds** of stable
capture metadata, a 10-second poll, and **no rsync bandwidth limit**. Its preset
is 5,000 steps, SH3, an initial seed capped at 300k measured points. The polite
non-demo defaults below retain 60 seconds of stability and bandwidth limiting.
Demo builds select at most **150 evenly spaced photos** from the latest moving
interval within roughly three minutes of its last movement. Only selected
photos are pulled and decoded, so long captures do not enlarge training or face
extraction. `--recent-frames` and `--recent-seconds` override these limits;
`--recent-frames 0` retains the original full-drive selection.

```sh
.venv/bin/python -u tools/postprocess/after_run.py --demo --watch \
  --host jetson-ts --since "$(date -u +%Y%m%dT%H%M%S)" --skip-quest --open-chrome
```

Use `--host jetson` for USB when `192.168.55.1` is reachable. On the 09:13 EDT
validation it timed out; the armed task watcher uses `jetson-ts` instead.
Omit `--watch` for one newest finished run. `--steps` and `--seed-max-points`
override the demo preset. This remains an image-idle heuristic, rather than a
motor stop signal. Final seed export must also finish before the pull starts.
The watcher excludes captures older than `--since`; arm it before the drive.
With `--open-chrome`, the completion notifier runs `open -a 'Google Chrome'`
on the complete walk URL (including `faces=`) once the final scene, sidecars,
optional pruning and scoring have all completed. It records the open
result in the timing receipt. Close room tabs before another build to avoid
competing for the Mac GPU.

Some deployed archives contain only images and transforms. In demo mode,
when neither `init.ply` nor `dense.ply` exists, the Mac retrieves the local
relay's retained measured RGB-D snapshot for the transforms' exact session and
map epoch. A different session or empty retained cloud cannot seed that run.
The receipt records the seed source. This uses the existing relay API and makes
no Jetson or relay changes.

The real 14:37 capture's recent snapshot used 150 photos and 171,331 measured
seed points: Jetson photo pull **10.07 s**, preparation **1.45 s**, Brush **124.07 s**,
CPU face extraction **19.13 s** overlapped, publication **0.06 s**, scoring
**1.81 s**, total **138.03 s** including seed retrieval/transfer. It produced
three face spots and **24.24 dB across 19 held-out views**. This supports the
roughly 2–2.5 minute judging budget under that measured load; transfer speed,
seed size, cold startup and other GPU users still affect elapsed time.

The full model is atomically published **before** optional Quest pruning or
post-hoc PSNR scoring. The runner prints a `/scenes/walk.html` URL with its
matching transforms: WASD/drag, minimap, and recorded-path tour. `--skip-quest`
removes pruning entirely. `--desktop-name NAME` sets its filename (otherwise
`<run-id>-desktop.ply`). Only scene artifacts and the existing walkthrough are
copied into `dist/scenes/`; relay/headset sources are untouched. The walkthrough
renders SH2; the published PLY retains all trained SH3 data.
The alternate `/splat.html` URL is included in the receipt.

Receipts distinguish `desktop_ready_s` (pull + prep + Brush + desktop copy) from
`total_s` (also optional Quest work and PSNR scoring). Automatic runs also record
pull/training/URL UTC timestamps and `last_photo_to_ready_s`, using the Jetson
photo mtime; that last measure assumes the Mac and Jetson clocks agree. Seed
export and idle detection occur before the timed pull. Brush's own final
held-out rendering is included in `train_s`.

For repeatable local length trials, each output directory must be new:

```sh
.venv/bin/python tools/postprocess/demo_benchmark.py \
  ~/farsight-captures/run-20261011T094259 --durations 0 60 120 \
  --out .cache/autosplat/demo-trial-1
```

This labels its pull as a **local rsync stand-in**, not a tether measurement.
Prefix trials reuse the whole saved run's measured seed, so they do not reproduce
the seed available after an actual shorter drive. The validated 119-photo face-enabled preset measured 112.19 s GPU work with
61.58 s CPU extraction overlapped, 113.73 s room/sidecar ready. Adding 0.307 s
local rsync and 10 s idle gives 124.04 s; this excludes real transfer/browser
parsing and reuses the whole saved seed. See
[QUALITY_RECEIPT.md](QUALITY_RECEIPT.md) for timing and qualification.

## Face pins and offline walkthrough

Demo mode also enables **Dechante's** face pins from
[walk-face-detection 98cc0e2](https://github.com/azizu06/farsight/commit/98cc0e2a3df8c7793532a81a21d6477bf7553b22).
`--faces` enables them for a non-demo run. The CPU process starts alongside
Brush, uses the selected strict-decoded photos and measured seed, and is niced
10. The receipt records `faces_s`, its exit code, and any wait after Brush.
Successful results publish `<run-id>-faces/faces.json` plus boxed `faceframes/`;
the printed walkthrough URL includes `faces=`. Extraction failures retain a
log/error receipt while the room still publishes.

Set up the separate Python 3.12 environment **before** a drive:

```sh
uv venv --python 3.12 .cache/autosplat/faces-venv
UV_CACHE_DIR="$PWD/.cache/autosplat/uv-cache" uv pip install \
  --python .cache/autosplat/faces-venv/bin/python \
  -r tools/postprocess/faces/requirements-mac.txt
mkdir -p .cache/autosplat/face-models
curl -fL https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n-pose.pt \
  -o .cache/autosplat/face-models/yolo11n-pose.pt
```

`--faces-python` overrides the environment. First-use library/font/model startup
can be substantial; warm it on a saved run before timing a demo. The detector
uses pose-keypoint heads (three confident nose/eye/ear points), rather than named
identity. Dechante's algorithm is retained, with CPU/model-path integration and
handling for an empty result with no confidence array. In the viewer, selecting
a face jumps to its camera and opens the boxed photo; the box also appears on
the room/minimap. The local `vendor/` modules and their licenses are published
beside `walk.html`, so the walkthrough needs no CDN or font network access.

## One command

From the repository root, with the pinned Brush v0.3.0 Metal binary already
installed by `tools/splat/run.sh`:

```sh
uv pip install --python .venv/bin/python -r tools/postprocess/requirements-mac.txt
.venv/bin/python tools/postprocess/after_run.py run-20261011T094259
```

Omit the run ID to select the newest finished capture. `--host jetson-ts` uses
Aziz's Tailscale SSH alias; `--host jetson` uses USB. A run is eligible only after
**all observed captures** have unchanged image counts and modification times
for at least 60 seconds. Polling is one small SSH inventory every 30 seconds
(`--poll-seconds 30..60`). Incomplete captures without a seed also defer pulls
while changing. This is the requested capture-idle heuristic, not a motor-state
signal: a camera that stops producing frames for 60 seconds looks finished.

Only then does rsync pull `images/*.jpg`, `transforms.json`, and `init.ply` (or native `dense.ply`), with
`--bwlimit=2048` KiB/s (`--bwlimit` overrides it). Per-frame point files are not
transferred. Keep `tools/pull_capture.py` and other manual pullers stopped during
a drive; this runner does not control them. If SSH is unreachable, it uses an
already-saved local dataset with transforms and a measured seed (including its own
`.cache/autosplat/captures/` pulls); it cannot retrieve
missing files from a powered-off Jetson.

To process an already-finished saved run without contacting the rover:

```sh
.venv/bin/python tools/postprocess/after_run.py --local ~/farsight-captures/run-20261011T094259
```

Both native `images/` + `transforms.json` and the older Mac `photos/images/` +
`photos/transforms.json` layout work, with `init.ply` or native `dense.ply` at the run root. The latter is
aliased to `init.ply` in the prepared dataset; the measured point data is unchanged.

## Watch after every run

Start once from a stable checkout:

```sh
mkdir -p .cache/autosplat
nohup .venv/bin/python -u tools/postprocess/after_run.py --watch \
  --since "$(date -u +%Y%m%dT%H%M%S)" \
  > .cache/autosplat/watcher.log 2>&1 &
echo $! > .cache/autosplat/watcher.pid
```

Stop only this watcher with `kill "$(cat .cache/autosplat/watcher.pid)"`.
Processing is synchronous: runs never stack. A watcher lock prevents a second
watcher; a separate job lock prevents concurrent training. The watcher keeps
polling while a local benchmark owns the job lock, and defers transfer/build.
`benchmark.py` waits for automatic jobs and leaves an observation window between
trials. `--wait-for-gpu` lets a manual pipeline wait too. Keep one shared
`--work-dir` for all invocations. A successful
`<run-id>.done.json` receipt prevents repeats across watcher restarts. Failed
runs retain their logs/receipt and are retried on later polls. Offline mode can
process the saved local runs, newest first. `--since` excludes older archive
backlog when arming for the next rehearsal; omit it to process all unfinished runs. When online, each subsequent run
requires another idle observation before any pull.

## What it does

1. Strictly verify and decode JPEGs with Pillow; drop decode failures. Valid
   files shorter than Brush v0.3.0's fixed 16,387-byte initial read become
   lossless PNGs padded to 20k bytes, preserving their decoded pixels. Original
   JPEGs remain available for held-out scoring and in the archive.
2. Call Dechante's `find_drives.py` with `--drive-gap-seconds` (default 60).
   When there is one drive, retain all photos, including the beginning/end.
   Otherwise cut the longest segment by elapsed time. The standalone script
   retains its original 12-second default. A parked-only capture produces no room.
3. Copy the run's measured seed and set `ply_file_path: init.ply`. Use the full
   initial seed, matching Dechante's method. `--seed-max-points 300000`
   enables the measured Mac seed-capped alternative.
   Stock Brush's `--max-splats` only caps growth.
4. Run Brush through `nice -n 10`, 1600-pixel maximum resolution, SH3, at the
   selected total step count. Every eighth photo is held out; export/eval happen
   at the final step. These match Dechante's latest `train_steps.ps1`.
   `--load-test` adds an optional one-step diagnostic before training; it is
   disabled by default because cold initialization can be expensive.
   `--fast` selects 2,000 steps; `--train-sh-degree 1` selects cheaper SH.
   `--open-chrome` launches the completed room viewer only after training.
5. Atomically publish the complete SH3 model and matching transforms into
   `dist/scenes/`, copy `walk.html`, and immediately print its walkthrough URL.
   Then, unless `--skip-quest`, prune to 300k/SH1 using `tools.splat.prune` and
   atomically publish `dist/scenes/<run-id>.ply` for the headset.
6. Score the held-out renders with Dechante's `score_heldout.py`; write a JSON
   receipt with wait, pull, prep, train, prune, publish, total, steps/s and score.

`--steps` or `SPLAT_STEPS` sets the budget. `--brush` or `BRUSH` selects the binary.
`--max-resolution` changes training cost. Receipts contain a suggested 120-second
step count based on the measured whole Brush stage; the default is **5,000 steps**.
The exact full-seed 5k default measured **338 seconds** on this Mac, exceeding
the original two-minute target. `--fast` uses 2k steps; a fresh full-seed trial
was interrupted after 340.63 seconds to prioritize the requested cap rerun, so
the earlier throughput extrapolation is not a verified budget. The optional
`--steps 7500 --seed-max-points 300000` preset measured 129.46 s training /
135.18 s total earlier, at lower held-out PSNR. Its fresh 09:12 EDT rerun took
202.68 s in Brush / 207.92 s to desktop publication under the then-current shared
Mac load. This is not proof that GPU contention alone caused the difference.
Completion observation and remote transfer add to training time. Run another full validation when changing seed size, resolution, or
concurrent GPU load.

Defaults publish to Aziz's currently served Quest distribution:
`/Users/aziz.u/.treehouse/farsight-5a56e1/5/farsight/.cache/050-runtime/quest/dist/scenes/`.
Use `--dist /path/to/quest/dist` for a different checkout and `--base-url` for
its actual server origin (default `http://localhost:8080`). The printed path is
`/splat.html?scene=scenes/<run-id>.ply&type=ply&sh=1&flip=1&pos=x,y,z&yaw=degrees`.
The measured midpoint camera determines the XR start and yaw; `--eye-height`
(default 1.6 m) converts its position to the viewer's floor point. The model
and pose share the same upright flip. The receipt includes the matching capture
transforms for the viewer's interior start. On the headset use the relay's
reachable origin. This produces a scene link; it does not navigate a headset
page that is already open.

Runtime artifacts, datasets, exports, and timing receipts remain in ignored
`.cache/autosplat/`; no venue photos or point-cloud artifacts go to GitHub.
See [MAC_RECEIPT.md](MAC_RECEIPT.md) for the real saved-run timing and browser
qualification, including its limits.

```sh
.venv/bin/python -m unittest discover -s tools/postprocess/tests -v
npm --prefix quest run test:judge
```

## Opt-in turbo and seed experiments

Sai's fork is built in ignored `.cache/autosplat/` and never changes `rust/` or
his working branch. Build it before any drive; compiling is outside room timing:

```sh
git fetch origin splat-turbo
.venv/bin/python tools/postprocess/build_turbo.py
```

The helper pins [Sai's commit 34d2d416](https://github.com/azizu06/farsight/commit/34d2d416dfda207fdd89fd58ba58fd2bca154eb9),
uses release/native Apple CPU settings and direct Metal shader generation, and
writes its build receipt. The binary is `--trainer turbo`'s default. Stock Brush
uses `--total-steps`; the fork uses `--total-train-iters`. `--max-splats` caps
initial points in the fork too, unlike stock v0.3.0. `--brush-extra` must come last.
Sai's `--init-decimate` takes a **point count**, not a divisor: one third of the
094259 seed is 484,685. For example, after qualifying this config locally:

```sh
.venv/bin/python tools/postprocess/after_run.py --local ~/farsight-captures/run-20261011T094259 \
  --trainer turbo --steps 5000 --seed-max-points 0 --max-splats 1454055 \
  --skip-quest --brush-extra --init-decimate 484685 --refine-mode budget
```

`benchmark.py DATASET --out DIR --kind turbo --brush PATH --steps 2000 3000 5000`
records one measured training/scoring receipt per budget. Use `--max-splats` and
`--extra` to specify a matching seed/growth regime; changing the binary alone
can change the initial seed. These serial trials share the watcher's job lock.
They measure local training, not transfer or complete post-run latency.

`voxel_seed.py input-init.ply output-init.ply --voxel 0.045` merges measured xyz/RGB
samples into metric cells and reports point count and preparation time. It is
for the capture seed's binary xyz-f32/RGB-u8 layout, rather than trained Gaussian
PLYs. On the saved 1.45M-point seed, 4.5 cm yielded 589,158 points in 10.03 s;
5 cm yielded 479,576 in 10.39 s; 6 cm yielded 330,350 in 8.35 s. Those are seed
preparation measurements; a training-quality claim requires a scored trial.
