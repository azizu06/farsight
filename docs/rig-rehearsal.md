# Passive rig rehearsal and go/no-go report (#42)

`tools/rehearse_demo.py` tells a ready rig from a green simulator. It watches the
upstream while the Quest drives, keeps a bounded timestamped log, and prints one
report: every check is **PASS**, **FAIL** or **UNKNOWN** with evidence and time.
The verdict is **GO** only when every check passes. Missing physical checks, a
simulated or replayed source, a short run or stale evidence can never give GO.

## What it never does

Only `GET /health`, `/control/status`, `/camera.jpg` (headers only) on the
upstream and `/relay/health` on the relay. Any other path is refused in code. It
never opens `/ws/control`, sends motor commands, takes controller ownership,
replays commands or changes a network. Permits and session tokens are dropped
before anything is written. `--ssh user@host` is optional, explicit, BatchMode
(no password prompts), and reads only load average and thermal zones. No scans, no sudo.

## One-command rig day

```sh
# Primary phone-owned route: keep tools.phone_usb running on the Mac. Close the Quest/operator page first: qualify_link and
# pitch_metrics refuse to run while another control socket owner is connected.
python -m tools.rig_day --url http://127.0.0.1:18443 --relay http://127.0.0.1:8080 --out runs-rig/day1
python -m tools.rig_day --url ... --out runs-rig/day1 --dry-run            # print the exact commands, run nothing
python -m tools.rig_day --url ... --out runs-rig/day1 --skip preflight --skip detector
```

`tools/rig_day.py` runs the existing tools unchanged, in this order, each skippable
with `--skip STEP`: `preflight`, `ready`, `stream` and `detector` (`tools.measure`),
`qualify_link`, `pitch_metrics` (read-only default, no `--hold-permit`), then
`rehearse` (`tools.rehearse_demo record`, passive, `--rehearse-minutes`, default 15).
With `--relay`, qualify_link and pitch_metrics probe the relay; everything else
uses `--url`. `--seconds`, `--ready-timeout`, `--ready-skip`, `--ca-file`,
`--insecure`, `--ssh` pass through. Nothing in the default path sends a drive
command or holds a permit.

The folder gets one log per step (`02-ready.log`...), `qualify_link.json`,
`pitch.json`, `pitch.md`, `rehearsal/` (samples, report) and `summary.md` with
PASS/FAIL/SKIPPED per step. PASS means the tool exited 0, not that the rig is
qualified. The rehearse step is FAIL until the verdict is GO, so a default run is
NO-GO or INCOMPLETE until a human marks the physical checks (below). Exit 0 only
when no step failed.

## Run it

```sh
# Mac, while the Quest drives through the running phone USB helper and relay.
python -m tools.rehearse_demo record --url http://127.0.0.1:18443 --relay http://127.0.0.1:8080 --out runs-rehearsal/r1
# A human records each physical result as they watch it
python -m tools.rehearse_demo mark --dir runs-rehearsal/r1 motor_direction pass --by NAME --note "both wheels forward"
# Rebuild the report any time (also written by record on exit or Ctrl-C)
python -m tools.rehearse_demo report --dir runs-rehearsal/r1
```

Outputs in the directory: `samples.jsonl` (+ up to 2 rotated files, 2 MB each by
default; oldest dropped and counted), `meta.json`, `marks.json`, `report.md`,
`report.html` (dark), `report.json`. Exit code 0 only for GO. Interrupting
`record` still writes the report from what was recorded.

With `--relay`, samples retain its bounded `control_closures` diagnostics:
connection counters and at most eight endings per sample, including UTC time,
observed side/operation, reason and original numeric close codes. Unknown fields,
payloads and exception messages are excluded. Match those timestamps with
Jetson serial/kernel logs to diagnose held-driving interruptions. Older relay
versions without this field remain supported; bonded control is explicitly
excluded. These diagnostic observations do not change the GO checks or prove
why a connection ended. Log rotation still bounds total retained data.
Samples also retain the relay's boolean `demo_mode` selection when reported;
this records the relay configuration alone, without inferring the viewer or
Jetson configuration.

## Latency kinds are not interchangeable

| Kind | Source | If unavailable |
|---|---|---|
| Server capture age | `/health.camera_age_ms` | unknown |
| Relay acknowledgement latency | `control.link_ms`, only while the Quest drives | unknown |
| Browser/headset display age | not observable passively; read the HUD | unknown |
| Physical timer | human runs the timer test, then `mark displayed_video_age` | unknown |

## 15-minute loaded rehearsal

Rover wheels raised until motor direction is confirmed. Start `record` first, then:

1. **Cold startup** (human): full power cycle per [demo-checklist.md](demo-checklist.md); stopwatch to READY and VR video. `mark cold_startup`.
2. Drive normally with inference, H.264 and controls on. The logger supplies the software checks.
3. **Disconnects** (human, one at a time, restoring each): rover phone, operator phone, Quest USB, camera. Rover must stop (Jetson 250 ms lease). `mark phone_disconnect`, `quest_disconnect`, `camera_disconnect`.
4. **Encoder recovery**: restart H.264 on the Jetson; video returns, rover process stays. `mark encoder_recovery`.
5. **Held trigger**: hold the original trigger through a fault; it must stay stopped. Then release and re-press: motion resumes. `mark held_trigger_stays_stopped`, `release_repress`.
6. **Motor direction and starting PWM** on the floor. `mark motor_direction`, `starting_pwm`.
7. **Uno watchdog**: kill the Jetson link; the Uno stops itself within 300 ms (scope or video, not eyeballing). `mark uno_watchdog`.
8. **Power and temperature** after the full 15 minutes (Jetson in the intended power mode, `tegrastats` on the Jetson). `mark power_under_load`, `temperature`.
9. **Displayed video age** with a physical timer in frame. `mark displayed_video_age`.
10. **Seek arrival**: Seek/Explore drives to the target and stops at the standoff. `mark seek_arrival`.
11. **Range stop**: drive at an obstacle; stops at 25 cm by tape measure. `mark range_stop_25cm`.
12. **Tailscale direct**: `tailscale status`/`tailscale ping` shows a direct path, not DERP. `mark tailscale_direct`.
13. **Quest mic and playback**: push-to-talk reaches the assistant; reply or phrase is audible. `mark quest_mic_playback`.
14. **Wi-Fi off run**: venue Wi-Fi off (cellular/tether only), video, control and stop still work. `mark wifi_off_run`.

Measured software checks come from the logger; the marked rows are human
observations and say who marked them and when. Physical gates in #11, #12 and
#5 stay open until the real rig has produced GO evidence.

## Phone-first setup and current profile

Use this profile for the rehearsal; see [jetson-bringup.md](jetson-bringup.md) and
[ios/README.md](../ios/README.md) for the full steps:

```sh
bash jetson/install_service.sh --dry-run --camera-source phone --serial-port ''  # review
bash jetson/install_service.sh --camera-source phone --serial-port ''
```

The empty serial keeps motors disabled; for the motor tests set the real Uno
port afterward. `FARSIGHT_CAMERA` stays blank for the phone source.
The Jetson flash is not confirmed by this tool or by the old handoff
([jetson-flash-handoff.md](jetson-flash-handoff.md)): confirm boot with `tools.ready`
and `tools.preflight` on the device before relying on it.

## Software smoke test

```sh
python -m jetson.server --demo --simulate-rover --port 18443 &
python -m tools.rehearse_demo record --url http://127.0.0.1:18443 --out /tmp/r --minutes 0.2 --min-minutes 0.1
```

Expected: NO-GO with `real_rig` FAIL (simulated), physical rows UNKNOWN. That is
the tool working. With the server down, `reachable` fails and nothing is invented.
