# Link lab: cellular control and video under emulated profiles

`tools/link_lab.py` runs the real operator path through emulated cellular
links and reports what the operator would feel while holding the deadman.
**All numbers on this page are software evidence from emulation on a Mac, not
hardware results.** The real-path recipe at the end is how to check them.

## What it runs

```text
lab clients (rover.ts-equivalent control + h264.ts-equivalent video, window 2)
  -> tools.operator_relay (production relay, own process)
  -> emulated cellular leg (user-space localhost TCP proxy, own process)
  -> jetson.mapping.server fixture (real FFmpeg H.264 at 10 fps / 500 kbps,
     synthetic RGB, simulated motors), or a real server via --upstream
```

The emulated leg is one shared bottleneck per direction, like the phone
Tailscale tunnel: every TCP connection shares its serializer, delay, stalls and
loss. Each packet gets base delay + exponential jitter + any active stall; a lost
packet arrives one retransmission timeout late, and TCP order is kept, so later
bytes on that connection wait behind it. It never changes OS networking.

The lab client mirrors the Quest's own rules: the 250 ms permit gate in
`control-safety.js` (no driving on a permit received 200 ms ago or more), the
500 ms view guard, the release latch, and the h264.ts reset after a request is
1 s overdue followed by a 1 s reconnect. After each forced release it lets go
for 300 ms and presses again, like an operator.

```sh
python -m tools.link_lab --calibrate --profiles derp_typical          # stop-only probe vs field numbers
python -m tools.link_lab --profiles direct derp_typical derp_bad booth --report /tmp/link-lab.json
python -m tools.link_lab --sweep --sweep-jitter 12 16 20 25           # stutter threshold
python -m tools.link_lab --permit-hz 20 --policies periodic20         # the pre-change behaviour
python -m tools.link_lab --server-options '{"video_bitrate": 250000}' # encoder variants
python -m unittest tools.tests.test_link_lab
```

Held-drive trials refuse anything but the simulated fixture. With
`--upstream` the lab only runs the stop-only probe (`tools.qualify_link`
method) plus video, never motion.

## Profiles

Calibrated with `--calibrate`. The probe sends `deadman:false` at 10 Hz and
times the first status echoing its `seq`, so it includes the 0–50 ms wait for
the next status tick, exactly like the field measurement.

| Profile | Emulation | Probe p50 / p95 / max |
|---|---|---|
| `direct` | 22 ms each way, 3 ms jitter, 8/4 Mbps | 91 / 102 / 109 ms |
| `derp_typical` | 26 ms each way, 20 ms jitter, stalls 80–200 ms at 0.5 Hz, 6/3 Mbps | 117 / 212 / 343 ms (field over DERP Miami: 118 / 217 / ~350) |
| `derp_bad` | as typical, stalls 200–380 ms at 0.4 Hz, 2% loss (250 ms RTO), 4/2 Mbps | 143–155 / 455–468 / 560–700 ms |
| `booth` | 40 ms each way, 30 ms jitter, stalls, 1% loss, 0.9/0.5 Mbps, 600 ms buffer | 174–178 / 325–396 / 557 ms |
| `none` | pass-through, for real-path probes | local fixture 34 / 52 / 53 ms; real Jetson over USB 46 / 81 / 129 ms |

Real phone-relay path over DERP (formal rig run 2026-10-10, 20 Hz permits,
before this change): control RTT p50/p95/max 123/182/270 ms, 4 `expired_permit`
stops per 30 s, max permit gap 228 ms. That sits between `direct` and
`derp_typical`, above the pre-change stutter threshold below.

The `none` rows were taken with 20 Hz permits. The real Jetson over its USB link
(sub-millisecond ping) already adds 10–30 ms over the local fixture at p50/p95:
its own scheduling under inference load spends lease budget before any cellular
hop. Measured 2026-10-10 over `http://192.168.55.1:8443`, stop-only, with the
rover phone unplugged.

## Results (60 s held drive per row)

"Before" is the code on main at the start of this work: Quest sends on a 20 Hz
timer with the newest permit, and the server issues permits at 20 Hz. "After" is
this change: Quest also sends as soon as a new permit arrives while driving,
and the server issues permits at 40 Hz. The lease stays 250 ms; the release latch
and view guard are unchanged.

*Interruptions* are forced releases (lease expiry, expired permit, Quest permit
gate, stale view), each one needing a new trigger press. *Headroom* is how much
of the rover's lease was left when each renewal arrived (p5); at zero it stops.

| Profile | Version | Interruptions/min | Moving while held | Drive between stops | Lease headroom p5 | Video fps / age p50 |
|---|---|---:|---:|---:|---:|---:|
| direct | before | 0 | 100% | — | 93 ms | 10.0 / 199 ms |
| direct | after | 0 | 100% | — | 165 ms | 10.0 / 201 ms |
| derp_typical | before | 43 | 67.8% | 0.9 s | 24 ms | 10.0 / 271 ms |
| derp_typical | after | 31 | 78.5% | 1.5 s | 51 ms | 10.0 / 269 ms |
| derp_bad | before | 48 | 6.8% | 0.1 s | 3 ms | 7.6 / 554 ms |
| derp_bad | after | 33 | 7.9% | 0.1 s | 42 ms | 7.3 / 631 ms |
| derp_bad | after + 250 kbps video | 75 | 18.8% | 0.2 s | 22 ms | 9.5 / 403 ms |
| booth | before | 17 | 1.0% | — | 12 ms | 6.0 / 827 ms |
| booth | after | 21 | 1.4% | — | 8 ms | 5.7 / 698 ms |
| booth | after + 250 kbps video | 37 | 3.6% | — | 9 ms | 7.6 / 659 ms |

A second seed reproduced the DERP-typical result: before 41/min, 70.1% moving,
headroom p5 22 ms; after 35/min, 75.3% moving, headroom p5 55 ms. Interruption
counts vary by a few per minute between runs; headroom moves consistently.
Raw reports are in `docs/qualification/link-lab-2026-10-10.json`.

Interruptions per minute fall at the worst profiles only because the rover is
rarely re-armed; read those rows by "moving while held". In `derp_bad` and
`booth` most stops are the 500 ms view guard: video age is mostly network, and
the fixture's capture-to-send age is already about 200 ms.

## Stutter threshold

Jitter sweep on a fixed 50 ms base RTT (no stalls, no loss), 45 s per point:

| Mean jitter each way | Probe p95 before / after | Before: interruptions/min (moving) | After: interruptions/min (moving) |
|---|---|---|---|
| 12 ms | 139 / 137 ms | 1.3 (99.0%) | 0 (100%) |
| 14 ms | 165 / 133 ms | 1.3 (99.2%) | 1.3 (99.3%) |
| 16 ms | 159 / 148 ms | 5.3 (96.0%) | 0 (100%) |
| 18 ms | 169 / 149 ms | 10.7 (91.8%) | 2.7 (98.1%) |
| 22 ms | — / 183 ms | — | 6.7 (94.9%) |
| 25 ms | — / 178 ms | — | 20 (87.3%) |
| 30 ms (30 s run) | 224 / 203 ms | 58 (58.4%) | 52 (61.8%) |

Stutter (5 or more forced releases per minute) starts at about 16 ms mean
jitter each way before and about 22 ms after. As a network round-trip p95
(base 50 ms + jitter, without the probe's status wait) that is **about 125 ms
before and about 155 ms after**; as accepted permit age (`link_ms`) p95, about
150 ms before and 170 ms after. Probe readings depend on the server's permit
cadence: the same link reads 10–25 ms lower at 40 Hz (direct: 91/102 at 20 Hz,
64/79 at 40 Hz). The field DERP measurement (probe p95 217 ms at 20 Hz, so
roughly 190 ms network p95) is above both thresholds; a direct path is well
below them. With a 250 ms lease that never lengthens, no client or server
cadence can carry a 200 ms network stall: the next permit cannot arrive in time.

## What changed and why

- **Quest sends on each new permit** (`quest/src/rover.ts`). Waiting for the
  50 ms timer spent up to a fifth of each permit. The extra send uses the
  same `pump()` decision as the timer, so every gate still applies. No wire
  change: old servers just see more heartbeats.
- **Permits at 40 Hz** (`jetson/control/service.py`, `STATUS_INTERVAL`). A
  renewal now needs a round trip under about 225 ms instead of 200 ms. Status
  is about 420 bytes, so control costs about 135 kbps down and 80 kbps up at
  40 Hz. Mission replay still records control at 20 Hz.
- Not changed: the 250 ms lease, the 500 ms view guard, the release latch, the
  relay, the request window, and timeouts.

The relay already gives control its own upstream connection pool and its own
WebSocket, so video cannot head-of-line block control inside the relay. The
remaining head-of-line blocking is in the shared cellular bottleneck, where the
fix is a smaller video load, not more connections.

## Recommendations (not done here)

1. **Get a direct Tailscale path; it matters more than any code change.** The
   direct profile never stopped. DERP-class jitter stops the rover every 1–2 s even after
   this change. Check with the recipe below before rig day.
2. **Start cellular demos at 250 kbps video** (`FARSIGHT_VIDEO_BITRATE=250000`
   in `/etc/farsight.env`; not changed by this work). On `derp_bad` it raised
   video from 7.3 to 9.5 fps, cut view age p50 from 631 to 403 ms and encoder
   resets from 8 to 1 per minute.
3. **A booth-class link (<1 Mbps up, bufferbloat) cannot satisfy the 500 ms view
   guard**; plan on the recorded/simulated fallback there rather than tuning.
4. Decision for the humans, not implemented: every lease gap currently requires
   a new trigger press. Allowing motion to resume automatically while the
   deadman is still continuously held would turn most DERP stutters into short
   coasting stops, but it changes the "explicit release after faults" rule.

### Rust server parity (for Devin)

- `rust/farsight-server/src/server.rs` control sender loop: sleep to a
  **0.025 s** period instead of 0.05 s, and call `control_on_status` at most
  every 0.05 s (Python: `STATUS_INTERVAL` / `RECORD_INTERVAL` in
  `jetson/control/service.py`). Permit validity stays `now + LEASE` (250 ms),
  `permit_ms` stays 250, and status fields are unchanged.
- Set `TCP_NODELAY` on accepted sockets in both serve paths (`axum::serve`
  listener and `axum_server::bind_rustls`). Neither sets it today, so Nagle can
  hold a 40 Hz status behind an unacknowledged one on a high-RTT direct
  Tailscale path. Python on the Jetson already gets `TCP_NODELAY` (checked on
  the rig: `uvicorn` accepted sockets report 1). Not measured: the lab proxy
  terminates TCP on loopback, so it cannot show Nagle effects.

## Real-hardware recipe

Emulation only predicts. On the real rig, with both phones on cellular and
Wi-Fi off:

1. **Path type and RTT.** On the Jetson (it shares the rover phone's cellular
   path): `tailscale ping -c 30 <operator-phone-tailscale-ip>`. "via DERP(mia)"
   means relayed; "via <ip>:<port>" means direct. Record min/median/max.
   `tailscale netcheck` shows the nearest DERP and whether NAT mapping varies by
   destination (hard NAT tends to force DERP). Post only path type and RTTs, never
   device IDs.
2. **Probe the real path from the operator Mac** with the phone relay up
   (`docs/phone-relay.md`), stop-only and without emulation:

   ```sh
   python -m tools.link_lab --upstream http://127.0.0.1:18443 --profiles none --probe-seconds 60 --report /tmp/link-real.json
   ```

   For the computer-side fallback use the Jetson's tailnet origin instead.
   Compare the probe p50/p95 to the profile table and the threshold above
   (the server now issues permits at 40 Hz, so subtract nothing; the 20 Hz
   field numbers read 10–25 ms higher). A 40 Hz probe p95 under about 170 ms
   predicts smooth held driving; over about 200 ms predicts stops every second
   or two.
3. **Held drive with wheels off the ground**, 60 s, Quest trigger held: count
   release prompts and watch `link_ms` in the HUD/status. Compare with the lab
   row for the measured probe. Record source, load and conditions next to the
   numbers.
