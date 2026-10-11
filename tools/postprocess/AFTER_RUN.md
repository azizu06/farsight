# Automatic Mac rooms after a drive

This runner builds on **Dechante's** `splat-fast-steps` tools, refreshed
from [commit 217bb22](https://github.com/azizu06/farsight/commit/217bb22) (2026-10-11). [README.md](README.md) retains the original
workflow and RTX 4070 results. Mac automation lives in `after_run.py`; it does
not change the Jetson, renderer, relay, or headset app.

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
Processing is synchronous: runs never stack. An advisory lock prevents a second
watcher or one-command invocation using the same work directory from training
concurrently. Keep one shared `--work-dir` for all invocations. A successful
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
   Brush's `--max-splats` only caps growth.
4. Run Brush through `nice -n 10`, 1600-pixel maximum resolution, SH3, at the
   selected total step count. Every eighth photo is held out; export/eval happen
   at the final step. These match Dechante's latest `train_steps.ps1`.
   Run a one-step Brush load test before training and retain its log/timing.
   `--fast` selects 2,000 steps; `--train-sh-degree 1` selects cheaper SH.
   No viewer or live provider is launched.
5. Prune to 300k/SH1 using `tools.splat.prune`, atomically copy via temporary file
   and rename into `dist/scenes/<run-id>.ply`, then print the viewer URL.
6. Score the held-out renders with Dechante's `score_heldout.py`; write a JSON
   receipt with wait, pull, prep, train, prune, publish, total, steps/s and score.

`--steps` or `SPLAT_STEPS` sets the budget. `--brush` or `BRUSH` selects the binary.
`--max-resolution` changes training cost. Receipts contain a suggested 120-second
step count based on the measured whole Brush stage; the default is **5,000 steps**.
The exact full-seed 5k default measured **338 seconds** on this Mac, exceeding
the original two-minute target. `--fast` uses 2k steps (predicted ~135 s from
that throughput; not independently timed). The optional
`--steps 7500 --seed-max-points 300000` preset measured 129.46 s training /
135.18 s total, at lower held-out PSNR. The 60-second completion observation,
one-step load test and remote transfer add to training time. Run another full validation when changing seed size, resolution, or
concurrent GPU load.

Defaults publish to Aziz's currently served Quest distribution:
`/Users/aziz.u/.treehouse/farsight-5a56e1/5/farsight/.cache/050-runtime/quest/dist/scenes/`.
Use `--dist /path/to/quest/dist` for a different checkout and `--base-url` for
its actual server origin (default `http://localhost:8080`). The printed path is
`/splat.html?scene=scenes/<run-id>.ply&type=ply&sh=1&flip=1&pos=x,y,z&yaw=degrees`.
The measured midpoint camera determines the XR start and yaw; `--eye-height`
(default 1.6 m) converts its position to the viewer's floor point. The model
and pose share the same upright flip. Desktop still forces an orbit view; its
renderer does not honor an inside-camera start. On the headset use the relay's
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
