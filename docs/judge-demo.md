# Judge demo: 90-second live sequence (#43)

One coherent story for a 6-minute judging slot ([rules](hackers-guide.md): 2 min setup, 6 min
judging + Q&A, 2 min teardown). The 90 s sequence is the core; the rest of the slot is Q&A and
the optional beats at the bottom. Failures: [fallback-playbook.md](fallback-playbook.md). What may
be claimed: [devpost-checklist.md](devpost-checklist.md). Design: [architecture.md](architecture.md).

**Hardware status when this was written (2026-10-10 morning):** the Jetson does not yet accept SSH
([jetson-setup-handoff.md](jetson-setup-handoff.md)), and no Quest, rover, phone or motor run has
been qualified. Everything below is the plan for the *qualified* rig. If a gate has not passed on
the day, use the matching fallback tier and say so. Never narrate simulation or replay as live.

## Automated smoke test (simulator + replay, #57)

Run before every rehearsal and after pulling main:

```sh
npm --prefix quest run test:judge          # ~15 s; add -- --out report.json for a JSON copy
```

`quest/tests/judge-flow.e2e.mjs` starts `python -m tools.demo --simulate --build` and a
`--replay tools/fixtures/replay-demo` server behind its own relay on free ports, drives the served
page in headless Chromium/SwiftShader with the desktop controls (Space deadman, W, E, click,
Settings), and prints PASS/FAIL per step: page load, SIMULATED banner, video frames, detections,
deadman drive and release stop, Explore, Seek, drill start/score, Ask (text), mission report,
scan, REPLAY banner with inert motors, never LIVE. It exits non-zero on any failure. Uses
`.venv/bin/python` if present (override with `FARSIGHT_PYTHON`) and needs `npm --prefix quest ci`.

What it does **not** prove: the ray-cast room has no people, so one synthetic person box
(`id 900001`) is appended to the real detector packets to test rendering and click-to-seek; the
server never tracks that person. XR controller paths are covered separately by
`npm --prefix quest run test:browser`. Every result is SIMULATED or REPLAY, never hardware evidence.
On a heavily loaded machine a headless render stall can trip the viewer's 250 ms display guard
(a FAIL whose HUD says "Release deadman to continue"); re-run once before treating it as a regression.

**Known open bugs (found by this test, not fixed here), so `scan` fails often:**

- *Viewer cancels a completed scan.* The sweep ends with `stop("scan_finished")`
  (`jetson/control/state.py` `end_scan`); the operator's next held heartbeat then hits
  `requires_release` and overwrites the reason with `release_required` before the 50 ms status tick
  (`jetson/control/service.py`). `quest/src/rover.ts` only sets `scanCompleting` for reason
  `scan_finished`, so it shows "Scan cancelled" and never opens the 3D map even though `/scan` says
  `complete`. Timing does not depend on scan length, so the real rig is affected too. FAIL text:
  `viewer cancelled a scan the server completed`.
- *Server aborts at view 0 (seen less often).* `server aborted scan after 0 views: Scan cancelled:
  operator lease, override, or safety stop`, with the control reason back at `active`. Likely cause:
  the zero-stick `teleop` heartbeat sent just before `POST /scan` reaches the rover after
  `begin_scan` (separate WebSocket and HTTP paths through the relay) and is treated as
  `scan_override`.

## Roles (4 people)

| Role | Does | Hands |
|---|---|---|
| **Operator** | Wears the Quest, drives, selects the person | Right trigger, left stick, right A |
| **Spotter** | Plays the person to find; lies down on cue | Stays out of the rover's path |
| **Presenter** | Talks, runs the MacBook browser (`localhost:8080`) for Ask and Report | MacBook |
| **Rig tech** | Rover reset, power, E-stop reach, watches `tools.ready` and the HUD | Next to the rover, hand near battery switch |

The rig tech may kill rover power at any moment. Say this to judges: safety is not a demo prop.

## Start state (before judges walk in)

Do the cold boot in [demo-checklist.md](demo-checklist.md) until `tools.ready` prints `READY`, then:

- [ ] Quest in VR on `http://localhost:8080`, camera view (not map), HUD shows video age and **no** `SIMULATED` or `REPLAY` badge.
- [ ] Rover on the floor, camera centred (left X once), front range clear, ~2 m of free floor ahead, a wall or table leg visible in the distance.
- [ ] Spotter standing 3-4 m ahead, in the rover's view, **not** moving yet.
- [ ] Trigger released. Nothing is held. Detector HUD shows 0-1 detections.
- [ ] MacBook browser open, Settings closed, mission report empty or from a previous run you can name.
- [ ] Reset between runs: new `--mission-dir` run (restart the `farsight` unit) so the report only shows this demo.

## Controls (current main, `quest/src/rover.ts`)

| Input | Action |
|---|---|
| **Right trigger (hold)** | Deadman. Release stops the rover; Jetson lease is 250 ms |
| **Left stick** | Drive (forward capped at 45% of the PWM ceiling) |
| **Head turn** | Pans camera (servo, or digital crop if the camera is fixed) |
| **Right A**, controller ray on a tracked person's box | **Seek** that person |
| **Right A**, ray anywhere else | Toggle live camera / 3D map (stops driving; re-press trigger) |
| **Right B** | Toggle Explore (live view only) |
| **Left X** | Centre pan |
| **Left Y** (with trigger held, sticks centred) | Camera-only 3D scan |
| **Left grip** | Start the 90 s rescue drill |
| **Left trigger (hold)** | Ask the local assistant by voice; release to send. Enable in Settings first. Confirms a pending proposal |
| Any stick input during Seek/Explore | Takes over; autonomy stays off until trigger release |

`quest/README.md` and `README.md` match this table; `quest/src/rover.ts` stays authoritative,
so re-check it right before rehearsal.

## The 90 seconds

| Time | Operator does | Presenter says | Proof shown |
|---|---|---|---|
| 0:00-0:10 | Put on / already in headset, trigger **released** | "This is a rover you inhabit. The Jetson on its back does the seeing and the safety." | Live camera on the panel, HUD freshness |
| 0:10-0:25 | Turn head left and right | "My head pans the camera." Spotter steps in view. "Each box is a tracked person with a stable id and a pose." | Boxes and keypoints on matching frames; id stays while spotter walks |
| 0:25-0:40 | Hold **right trigger**, nudge **left stick** forward, then **release** | "Deadman: let go and the Jetson cuts the motors. A bad link does the same." | Rover moves, stops on release |
| 0:40-0:55 | Hold trigger, aim right controller at the spotter's box, press **right A** | "Seek: I choose a person, the rover drives itself, slows near them, and stops on arrival." | HUD autonomy state `tracking` then `arrived`; rover stops ~60 cm out |
| 0:55-1:05 | Release trigger. Spotter lies down | "Pose flags a person who looks down." | `down` flag on the box (pose detector only) |
| 1:05-1:20 | (Operator hands headset to a judge, or keeps it on) Presenter opens Settings on the MacBook, types **"What do you see?"** | "Local assistant, no internet. It answers from live detections, and a small vision model if loaded." | Text answer with timing; says it is offline |
| 1:20-1:30 | Presenter clicks **Open mission report** | "Everything it found is logged where the sensors are." | Per-person entries, snapshot, SBAR note, range, time |

Cue the spotter, not the clock: if Seek is slow, extend that beat and drop 1:05-1:20 rather than rushing.

### Say these limits out loud (one sentence each, no apologies)

- Counts are **person tracks / encounters**, not verified distinct people.
- Pose "down" is a geometric heuristic on keypoints, not a medical assessment. SBAR text is a model draft, not triage.
- Seek arrival is a **software bound** from the front ultrasonic (nearest obstacle, not necessarily the person), not an accuracy claim.
- Video age on the HUD is receipt-to-display software age, **not** measured camera-to-eye latency.
- Remote driving needs the cellular/Tailscale link; models run offline, the *link* does not.

## Optional beats (Q&A or spare time; each only if its gate passed)

| Beat | How | Gate before showing |
|---|---|---|
| **Explore** | Hold trigger, **right B**. Rover wanders, hands over to Seek on a fresh person | Explore qualified on the real rover with range check |
| **Scan** | Hold trigger, sticks centred, **left Y**; wait for the 5-view sweep, snapshot opens; **right A** off a person toggles map | Depth model on the Jetson (Mac only so far); label "estimated depth, not LiDAR" |
| **Phone LiDAR map** | Pro iPhone capture app to `/phone`; show the 3D map | Needs a LiDAR iPhone paired and routed; an ordinary iPhone gives RGB only |
| **Rescue drill** | **Left grip**; 90 s timer; faces held in view 1 s count as found; route-home summary | Faces model loaded; real people as victims; reports "observed face tracks" |
| **Speak** | Settings, phrase picker, Say EN / Say ES | USB speaker verified; otherwise the browser plays it and says so |
| **Voice ask** | Settings, Ask by voice, approve mic; or Enable in-VR voice, then hold **left trigger** in VR | Mic, permission and in-headset playback checked on the physical Quest (push-to-talk is desktop/synthetic-XR tested only) |

## What not to attempt live

- Driving at a wall to "prove" the e-stop unless the rig tech has tested range-stop at that speed that day.
- Anything needing a model that was not pushed to the Jetson (`tools/push_models.sh`).
- Raising any timeout to hide a slow link ([AGENTS.md](../AGENTS.md)). If video is stale, the rover is *meant* to stop; say so and move to the fallback.

## UI polish proposals (for the Quest owner, after P0 integration)

No Quest source is edited by this task. Candidates for the #40 owner, in priority order:
1. In-headset hint line for the A/B/X/Y/grip bindings (docs disagree today).
2. Persistent `SIMULATED` / `REPLAY` banner large enough to read from judges' distance on the mirrored view.
3. Show the Seek `arrived` state as a distinct HUD colour.
