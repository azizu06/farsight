# Demo video plan (#15)

Record this Sunday morning, before the Devpost deadline at **Sun Oct 11, 10:00 AM ET**
([hackers-guide.md](hackers-guide.md)). The guide only says "demo video attached"; it states no
length. Devpost videos are usually 2-3 minutes, so this cut is **2:45**.

The live sequence it follows is [judge-demo.md](judge-demo.md). Failures use
[fallback-playbook.md](fallback-playbook.md). What may be claimed is
[devpost-checklist.md](devpost-checklist.md); the prose is [devpost-draft.md](devpost-draft.md).
Every number below is copied from the "Measured so far" tables in
[pitch-metrics.md](pitch-metrics.md), and the row is named next to it.

## How to read a shot

| Column | Means |
|---|---|
| On screen | What the camera or the capture sees, including the source banner |
| Say | The voiceover line, spoken as written. Short sentences. |
| Source | **Live rig** (real rover, real camera, motors free), **bench rig** (real rover, wheels lifted, or camera live with motors off), **labelled replay** (`REPLAY` banner), **labelled simulator** (`SIMULATED` banner) |

Lead with the strongest real-hardware moment. As of 2026-10-10 that is the Jetson itself: it
boots, accepts SSH, and the pose engine ran on its GPU
([pitch-metrics.md](pitch-metrics.md), pose TensorRT row; [judge-demo.md](judge-demo.md) hardware
status). No Quest session, motor spin, or cellular drive has been qualified. If that changes
before the shoot, move the 0:28 wide shot to the front and drop the "not yet" half of its line.

## Honesty rules (read before recording)

From [devpost-checklist.md](devpost-checklist.md) and [devpost-draft.md](devpost-draft.md):

1. A simulated or replayed segment shows **SIMULATED** or **REPLAY** on screen, and the voiceover
   says so in the same shot. Those words are drawn by the Quest page
   (`quest/src/source-banner.js`: `SIMULATED` and `REPLAY · RECORDED MISSION`), not by a markdown
   file. A shot of a saved doc needs the page beside it, or a title burned in during the edit.
   Either way it stays in frame; do not crop it.
2. Never say a **[SIM]** number as if it came off the rig. A laptop, Mac, or emulation row stays
   that. If the demo runs a fallback tier, name the tier ([fallback-playbook.md](fallback-playbook.md)).
3. Use only numbers from [pitch-metrics.md](pitch-metrics.md) "Measured so far". Do not quote
   the draft's other figures (15/15 judge flow, 0 collisions, 24.9 s Explore, the 2 Mbps link
   fix, the 46-74% CPU cut). They are not in that table.
4. Say "tracked encounters", not "people found". Pose "down" is a geometric guess, not a medical
   call. Video age on the HUD is software age, not camera-to-eye time. Glass-to-glass and floor
   stop distance are both **not measured** ([pitch-metrics.md](pitch-metrics.md), "Not measured
   yet").
5. Do not claim an ultrasonic or a pan servo. This rig has neither
   ([devpost-checklist.md](devpost-checklist.md)). Scan depth is estimated unless a LiDAR iPhone
   produced it. Nav2 has only run in simulation; leave it out of this video.
6. Design limits (250 ms permit lease, 300 ms Uno watchdog) may be named as design, never as a
   measured stop. Physical stop time is not measured.

## Shot list (2:45)

| Time | On screen | Say | Source |
|---|---|---|---|
| 0:00-0:12 | Title card, then a close, well-lit shot of the Jetson Orin Nano on the rover chassis, Uno cable and mounted phone visible. No UI. | "This is Farsight. A rover you inhabit from a headset. The seeing and the safety run on this Jetson, not on the headset." | Live rig |
| 0:12-0:28 | SSH terminal. Scroll the saved pose log, not a fresh run: [jetson-runtime-results.md](jetson-runtime-results.md) (20 zero-input runs, median 15.29 ms, p95 18.83 ms). Hold the numbers long enough to read. A live re-run prints new numbers that are not in the table, so do not speak them. | "On this board, the pose engine ran in a median of 15.29 milliseconds. Ninety-five percent of runs finished within 18.83. That is engine time only, not camera to eye." | Saved Jetson log on screen. [pitch-metrics.md](pitch-metrics.md), "Pose TensorRT raw inference" |
| 0:28-0:42 | Wide shot: rover on the floor, operator in the Quest, spotter standing 3-4 m ahead. Cut to the headset view with the **LIVE** banner and a video-age number. | "Here is the live rig. The banner says LIVE only when the rover and the camera are both proven. That number is software age, not the time from camera to eye. Camera to eye is not measured yet." | Live rig. [pitch-metrics.md](pitch-metrics.md), "Glass-to-glass latency: not measured" |
| 0:42-0:54 | Headset view. Operator turns their head; the frame pans. Spotter walks into view; a box and a stable id hold. | "Turn your head and the camera view pans with you. Each box is a tracked person, with an id that stays put while they walk." | Live rig. If boxes do not draw, drop this shot. Do not swap in `--simulate`: that room has no people ([judge-demo.md](judge-demo.md)), and its one fake box is not a tracked person. |
| 0:54-1:08 | Side camera on the rover plus the headset view. Operator holds the right trigger, nudges forward, then releases. Rover stops. Stop-reason panel visible. | "Hold the trigger and it drives. Let go and the Jetson cuts the motors. The permit lease is 250 milliseconds by design. We have not measured the stop on the floor yet." | Live rig. [pitch-metrics.md](pitch-metrics.md), "Physical stop time: not measured". If the floor is not clear, do this on the bench rig and say "wheels are lifted". |
| 1:08-1:24 | Gate first, off camera, wheels lifted, a person in view. Seek only locks the `person` class (`jetson/autonomy.py`), so a box will not work. No range sensor, so the only arrival is the person's box at least half the frame wide (`w` ≥ 0.5; height does not count). Then, on the floor, with the spotter standing: hold the right trigger, aim at their box, press right A, keep holding. HUD shows `tracking` then `arrived`. Do not say a distance. | "I pick a person and keep the trigger held. It drives itself and stops when their box is half the frame wide. No distance sensor. Stop distance is not measured yet." | Live rig. [pitch-metrics.md](pitch-metrics.md), "Stop distance after deadman release: not measured". If the lifted-wheel gate was not clean, skip this shot. |
| 1:24-1:36 | Release the trigger and wait until the rover is still. Only then does the spotter lie down. The `down` flag appears on the box. | "The pose flags a person who looks down. That is a guess from the keypoints. It is not a medical call." | Live rig. [judge-demo.md](judge-demo.md): release trigger, then the spotter lies down. |
| 1:36-1:50 | MacBook browser, Ask box, typed question "What do you see?", answer plus its timing line. Banner stays LIVE. Do not read that timing line as a benchmark. | "Ask it what it sees. The answer comes from the live detections. That timing is one answer, not a benchmark." | Live rig. [pitch-metrics.md](pitch-metrics.md), "Assistant latency on the Jetson: not measured". The vision models in the Mac comparison row are not installed on the board. |
| 1:50-2:00 | Mission report: per-person entry, snapshot, SBAR note, time. The note says range unavailable. | "Everything it saw is logged on the robot. Tracks, not a head count. And there is no range sensor on this rover, so the note says range unavailable." | Live rig |
| 2:00-2:12 | Saved fault table, not a fresh run: [qualification-results.md](qualification-results.md). A markdown file has no banner. In the edit, burn a full-width title "SIMULATED (laptop)" across the top, or show the Quest page (real **SIMULATED** banner, `quest/src/source-banner.js`) beside the table. Do not crop it. Name which fault is which. Do not inject a fault on the live rig. | "This part is simulated on a laptop, not the rover. Killing the encoder: 169 milliseconds. Pausing video: 333. The 43 was a dropped connection." | Labelled simulator, title added in the edit. [pitch-metrics.md](pitch-metrics.md), "Simulated stop after injected fault" |
| 2:12-2:22 | Saved scan log from [scan.md](scan.md), the 596.5 ms line. Same rule: burn "SIMULATED (Mac)" across the top, or show the Quest page beside the log. The page's own scan timer is a different run; do not point at it and call it 596.5. If you also show the map, say the two runs differ. | "Still simulated, on a Mac. The saved scan: 89,967 points, 596.5 milliseconds, synthetic frames. Estimated, not LiDAR." | Labelled simulator, title added in the edit. [pitch-metrics.md](pitch-metrics.md), "Scan, real depth model, synthetic frames" |
| 2:22-2:32 | **REPLAY banner fills the top of frame.** Recorded frames play; controls do nothing; HUD shows motors disabled. | "This is a replay of a run recorded earlier, not a live drive. The banner says REPLAY, and the motors stay off. The controls do nothing." | Labelled replay. See the fallback section: if no real run was recorded, skip this shot and say the replay fixture is simulated plumbing only. |
| 2:32-2:45 | Back to the rover, still and powered, operator standing beside it. End card: repo name, "safety runs on the robot". | "The link can drop. The rover is built to stop itself, and we say so when it does. That is Farsight." | Live rig |

Total 2:45. If a live shot fails, swap in the matching labelled shot from the fallback section
and keep the time. Do not stretch a shot to hide a stall, and do not lengthen any timeout.

### Optional inserts (only if the gate passed that morning)

Drop the Ask shot (1:36-1:50, 14 s) or the mission-report shot (1:50-2:00, 10 s) to fit one of these. Each needs its banner, and each is said out loud.

| Beat | Show | Say | Gate |
|---|---|---|---|
| Two-phone link | HUD link line, both phones with Wi-Fi off, hotspot on | "Over the two phones, with Wi-Fi off, control round trip was 119.5, 167 and 228 milliseconds. The stream held about 9 frames a second." | That exact run, [pitch-metrics.md](pitch-metrics.md), "Control round-trip p50 / p95 / max" and "Stream fps, H.264" under "Real phones and the Jetson". Not the older 123/182 or 9.1 rows. |
| LiDAR map | Map view, source labelled measured | "This map is measured LiDAR from the phone, not the estimated scan." | Pro iPhone paired and routed ([judge-demo.md](judge-demo.md) optional beats) |
| Rescue drill | Drill HUD: timer, found count, score | "A 90-second drill. A face held in view for one second counts as found. It counts tracks, not people." | Faces model loaded, real people as victims |

## 30-second cut

Same honesty rules. Four shots, no new claims.

| Time | On screen | Say | Source |
|---|---|---|---|
| 0:00-0:08 | Jetson on the rover, then the saved 15.29 / 18.83 ms log (same log as the 0:12 shot, not a fresh run) | "Farsight. A rover you inhabit. Pose on this Jetson: 15.29 milliseconds median. Engine time only." | Saved Jetson log. [pitch-metrics.md](pitch-metrics.md), "Pose TensorRT raw inference" |
| 0:08-0:18 | LIVE headset view, head pan, box on the spotter, deadman release, rover stops | "Live from the rover. Turn your head, see a person, drive, let go, and it stops. Floor stop time is not measured yet." | Live rig. Banner must read LIVE, not UNVERIFIED. |
| 0:18-0:25 | Saved fault table (same table as the 2:00 shot) with the burned-in "SIMULATED (laptop)" title, or the Quest page beside it | "This part is simulated on a laptop. Killing the encoder stopped it in software. Not the real floor." | Labelled simulator, title added in the edit |
| 0:25-0:30 | Rover, still, end card | "The safety runs on the robot, and we label everything that is not live." | Live rig |

## Capture checklist

Do this in order. The rig tech stays next to the battery switch the whole time
([judge-demo.md](judge-demo.md)).

### Before anyone presses record

- [ ] Cold boot from [demo-checklist.md](demo-checklist.md) until `python -m tools.ready --url http://<jetson>:8443 --require-tensorrt --require-drive-camera` ends `READY`. Board profile is plain HTTP.
- [ ] Smoke test, laptop only, not hardware evidence: `npm --prefix quest run test:judge` ([judge-demo.md](judge-demo.md)).
- [ ] Quest page is `http://localhost:8080/?mount=fixed&h264=1` ([quest-first-run.md](quest-first-run.md)). Fixed mount, because this rover has no pan servo. If the HUD reads *H.264 codec unsupported* or never shows *decoded frame verified*, drop `h264=1` and stay on JPEG. Quest 2 H.264 playback is not verified.
- [ ] Confirm the banner. LIVE needs a real camera source and a connected rover. If it reads `NOT VERIFIED LIVE`, read the detail line and fix that, or do not call the shot live.
- [ ] Floor check: about 2 m clear ahead, spotter 3-4 m out, trigger released ([judge-demo.md](judge-demo.md) start state).
- [ ] New `--mission-dir` so the report shows only this run.
- [ ] Proximity stop off, or forward driving is quietly blocked. This rover also has no pan servo, and the default `servo` mount blocks forward drive while the headset commands a pan ([rover/README.md](../rover/README.md)). Cold boot uses the service, so set `FARSIGHT_PROXIMITY_STOP=0` and `FARSIGHT_PAN_MOUNT=none` in `/etc/farsight.env` ([jetson/farsight.env.example](../jetson/farsight.env.example)) and `sudo systemctl restart farsight`. A hand-started server uses `--proximity-stop off --pan-mount none` instead.
- [ ] Phones charged, Quest charged, spare battery, second filming phone charged and storage free.

### Three captures, rolling at the same time

1. **Headset view.** Cast the Quest view to the MacBook (Quest casting), or photograph the panel the Quest renders. Frame the source banner in every shot. Never crop it. If the cast drops, fall back to the desktop viewer at `http://localhost:8080`; say "desktop viewer" in the voiceover.
2. **Phone screen recording** of the MacBook Ask box and the mission report, for shots 1:36-2:00. Same rule: banner in frame.
3. **Second phone filming the rover**, from the side, so the stop is visible as motion, not as a HUD line. 60 fps if the phone has it. This is the only honest picture of a physical stop, and [pitch-metrics.md](pitch-metrics.md) says that timing is not measured, so do not read a time off this clip unless you ran the section 3 protocol.

### Commands (real flags)

Operator path, from [demo-checklist.md](demo-checklist.md) and `tools/demo.py`:

```sh
python -m tools.demo --upstream http://<jetson>:8443 --quest
```

`--quest` runs `adb reverse tcp:8080 tcp:8080` and prints `adb reverse --list`. `--h264` is
rejected on an upstream; video is configured on the Jetson, not by this flag. Add `--ca-file` only
if that Jetson was installed with TLS. Add `--adb-serial SERIAL` when more than one device is
attached. Re-run the reverse after any USB reconnect.

Record one real run before judging, from [demo-checklist.md](demo-checklist.md). On the qualified
rig, phone camera (omit `--camera 0`):

```sh
sudo systemctl stop farsight   # hand-starting fights the service for :8443 and the Uno serial
python -m jetson.server --serial-port /dev/ttyUSB0 --h264 --record --mission-dir runs --proximity-stop off --pan-mount none
```

Drive the normal demo, then Ctrl-C. Copy the whole `runs/<UTC timestamp>/` directory. It holds
`frames/*.jpg`, `replay.jsonl` and `replay.json`. Check free disk first; recording writes every
JPEG.

Play it back, labelled, from [demo-checklist.md](demo-checklist.md) and [fallback-playbook.md](fallback-playbook.md):

```sh
python -m jetson.server --replay runs/<UTC timestamp> --h264
python -m tools.demo --upstream http://127.0.0.1:8443 --quest
```

`/health` must show `replay: true`, and both the page and the headset must show **REPLAY**.
Controls are inert. This is evidence, not a new rescue.

Simulator, from `tools/demo.py` (`--simulate` and `--upstream` cannot be combined; `--h264` is
allowed only with `--simulate`, and needs FFmpeg):

```sh
python -m tools.demo --simulate --build --h264 --quest
```

The page shows **SIMULATED**. `--build` needs `npm --prefix quest ci` already done. That command
passes no `--depth-model` (`tools/demo.py` has no such flag), so its scan is labelled synthetic
depth only ([scan.md](scan.md)). Do not read its on-screen timer as the 596.5 ms number.

The 596.5 ms scan is the saved log in [scan.md](scan.md). Re-running it prints new
numbers that are not in the table, so do not speak them. The command, for a check only:

```sh
python -m tools.benchmark_scan --model /path/to/depth_anything_v2_metric_indoor_small_308x406.onnx
```

The 169 / 333 / 43 ms stops are the saved table in [qualification-results.md](qualification-results.md).
They come from `python -m tools.tests.test_demo_qualification`, a laptop fixture, not a rig
command. Do not run it against the rover, and do not re-speak a fresh printout as those three numbers.

### File names

One folder outside the repo, for example `~/Desktop/farsight-demo-video/YYYYMMDD/`. Not `runs/`,
and not anywhere under this checkout: neither is in `.gitignore`, so one `git add` would commit
the mp4s. Keep originals; do not overwrite a take.

| File | What it is |
|---|---|
| `take-NN-live-jetson.mp4` | Board and the SSH timing shot |
| `take-NN-live-headset.mp4` | Cast or captured headset view, banner in frame |
| `take-NN-live-rover.mp4` | Second phone, side view of the rover |
| `take-NN-live-report.mp4` | Phone recording of Ask and the mission report |
| `take-NN-sim-stop.mp4` | Saved fault table from qualification-results.md. No banner in the file; the edit adds "SIMULATED (laptop)", or the Quest page is in the same frame |
| `take-NN-sim-scan.mp4` | Saved scan log from scan.md. No banner in the file; the edit adds "SIMULATED (Mac)", or the Quest page is in the same frame |
| `take-NN-replay.mp4` | Recorded-run playback, REPLAY banner in frame |
| `voiceover.txt` | The lines above, unchanged |
| `notes.txt` | Per take: source tier, banner seen, link (USB / two phones / DERP), and which pitch-metrics row any spoken number came from |

### If the rig fails

Say it on camera, then switch. Never present a lower tier as live
([fallback-playbook.md](fallback-playbook.md), [demo-checklist.md](demo-checklist.md)).

| What broke | Do | Say |
|---|---|---|
| Motors or floor not safe | Bench rig: wheels lifted, or `FARSIGHT_SERIAL_PORT` blank and `sudo systemctl restart farsight` for camera plus detections with motion off | "Wheels are lifted" or "motors are off. This is the live camera, not a drive." |
| Camera dead, a real run was recorded | Tier 2: the `--replay` commands above. Confirm `replay: true` and the **REPLAY** banner before rolling. | "The camera is down, so this is a run recorded earlier on the rover. Motors are off." |
| No real run recorded | Do **not** use `tools/fixtures/replay-demo` as rover evidence. It is 7 frames, about 1.4 s, simulated, empty boxes ([fallback-playbook.md](fallback-playbook.md)). Skip the replay shot. | Nothing. Cut it. |
| Jetson down, or nothing live | Tier 3: `python -m tools.demo --simulate --build --h264 --quest`. **SIMULATED** banner on every frame. | "The Jetson is not up. This is the simulator, not the robot." |
| Quest cast fails | Desktop viewer at `http://localhost:8080`, same banner. Drive from the browser only if the rig tech confirms the floor is clear. | "This is the desktop viewer, same link." |

After any switch: trigger released, restart the server or the replay so mission state matches the
tier you announced, and confirm `/health` (`replay`, `simulated`) matches the line you are about to
say ([fallback-playbook.md](fallback-playbook.md)).

## Edit pass (before upload)

- [ ] Watch once muted. Every replayed second shows the page's **REPLAY** banner. The fault-table seconds show "SIMULATED (laptop)", the scan-log seconds show "SIMULATED (Mac)", or the Quest page with its **SIMULATED** banner sits beside the doc. A cut that crops either one gets recut.
- [ ] Watch once with sound only. Every such second is also named in the voiceover.
- [ ] Grep the voiceover against [pitch-metrics.md](pitch-metrics.md) "Measured so far". Any number not in a row comes out.
- [ ] No shot calls a [SIM] row a rig number. The 169/333/43 ms line says "laptop". The 596.5 ms line says "Mac" and "synthetic".
- [ ] LIVE shots do not show `NOT VERIFIED LIVE`. If one does, relabel it or reshoot.
- [ ] Export the 2:45 and the 0:30. Upload the 2:45 to Devpost; keep the 0:30 for a room where the rig cannot travel.
