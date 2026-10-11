# Automatic Mac rooms after a drive

This runner builds on **Dechante's** `splat-fast-steps` tools, imported unchanged
from [commit f70255f](https://github.com/azizu06/farsight/commit/f70255f) (2026-10-11). [README.md](README.md) retains the original
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

Only then does rsync pull `images/*.jpg`, `transforms.json`, and `init.ply`, with
`--bwlimit=2048` KiB/s (`--bwlimit` overrides it). Per-frame point files are not
transferred. Keep `tools/pull_capture.py` and other manual pullers stopped during
a drive; this runner does not control them. If SSH is unreachable, it uses an
already-saved local dataset with transforms and init.ply (including its own
`.cache/autosplat/captures/` pulls); it cannot retrieve
missing files from a powered-off Jetson.

To process an already-finished saved run without contacting the rover:

```sh
.venv/bin/python tools/postprocess/after_run.py --local ~/farsight-captures/run-20261011T094259
```

Both native `images/` + `transforms.json` and the older Mac `photos/images/` +
`photos/transforms.json` layout work, with `init.ply` at the run root. Remote
captures must already contain `init.ply` as specified by the run-saver workflow.
The alternate native `dense.ply` archive is not converted by this runner.

## Watch after every run

Start once from a stable checkout:

```sh
mkdir -p .cache/autosplat
nohup .venv/bin/python -u tools/postprocess/after_run.py --watch \
  > .cache/autosplat/watcher.log 2>&1 &
echo $! > .cache/autosplat/watcher.pid
```

Stop only this watcher with `kill "$(cat .cache/autosplat/watcher.pid)"`.
Processing is synchronous: runs never stack. An advisory lock prevents a second
watcher or one-command invocation using the same work directory from training
concurrently. Keep one shared `--work-dir` for all invocations. A successful
`<run-id>.done.json` receipt prevents repeats across watcher restarts. Failed
runs retain their logs/receipt and are retried on later polls. Offline mode can
process the saved local runs, newest first. When online, each subsequent run
requires another idle observation before any pull.

## What it does

1. Strictly verify and decode JPEGs with Pillow; drop failures and files shorter
   than 16,387 bytes (Brush v0.3.0's fixed-header `early eof` case). Originals stay
   in the archive.
2. Call Dechante's `find_drives.py` and cut the longest segment by elapsed time.
   A parked-only capture produces no room.
3. Copy the run's measured seed and set `ply_file_path: init.ply`. Subsample the
   initial seed to at most 300k points; Brush's `--max-splats` only caps growth.
4. Run Brush through `nice -n 10`, 640-pixel maximum resolution, SH1, at the
   selected total step count. Every eighth photo is held out; export/eval happen
   at the final step. No viewer or live provider is launched.
5. Prune to 300k/SH1 using `tools.splat.prune`, atomically copy via temporary file
   and rename into `dist/scenes/<run-id>.ply`, then print the viewer URL.
6. Score the held-out renders with Dechante's `score_heldout.py`; write a JSON
   receipt with wait, pull, prep, train, prune, publish, total, steps/s and score.

`--steps` or `SPLAT_STEPS` sets the budget. `--brush` or `BRUSH` selects the binary.
`--max-resolution` changes training cost. Receipts contain a suggested 120-second
step count based on the measured whole Brush stage; the default is **5,000 steps**.
The 60-second completion observation and remote transfer add to that render
budget. Run another full validation when changing seed size, resolution, or
concurrent GPU load.

Defaults publish to Aziz's currently served Quest distribution:
`/Users/aziz.u/.treehouse/farsight-5a56e1/5/farsight/.cache/050-runtime/quest/dist/scenes/`.
Use `--dist /path/to/quest/dist` for a different checkout and `--base-url` for
its actual server origin (default `http://localhost:8080`). The printed path is
`/splat.html?scene=scenes/<run-id>.ply&type=ply`; on the headset use the relay's
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
