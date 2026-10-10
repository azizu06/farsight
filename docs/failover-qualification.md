# Failover qualification

Software evidence only. A green run here is not hardware qualification and not
a claim about the phones.

```sh
python -m tools.qualify_failover --output failover.json --markdown failover.md
```

One command. A full simulated run is under two minutes (about ten seconds
when ffmpeg is already installed). Every process it starts is killed on the
way out, including when a scenario raises, and on SIGTERM: the process group
is registered with atexit, and the signal handler is installed by `main()`,
not by importing the module. Ports come from the kernel (`bind` port 0); it
never takes 8080, 8443, or 8081.

## What it builds

```text
operator client
  -> tools/operator_relay.py          production relay, one upstream, unchanged
  -> path A (primary) or path B       two independently cut/stalled/restored paths
  -> python -m jetson.server          --demo --simulate-rover, no serial port
       --simulate-rover
```

The relay still takes one upstream. Failover is "which upstream to try next",
in `choose_upstream`. At connect time it actually tries each path: a GET
/health, timed, including a path already marked down. The connection-time gap
is the time the dead path took to refuse, measured, not padded. A cut port
refuses in well under a millisecond and that is the number the report uses.
`probe_timeout_s` (0.4) is only the cap for a path that never answers, and it
is its own field in the report. A gap that matches that cap fails the run.

`choose_upstream(..., probe_all=True)` keeps probing after the first path
answers, because the report wants both tries. A phone bridge binding to it
must pass `probe_all=False`, or every connect also waits on the fallback
probe. That function is the seam for the phone bridge's fallback interface.
It is not bound to one yet. An open TCP/WebSocket is never moved.

`tools/fault_proxy.py` does abort the client socket on `drop()` (it tracks the
accepted writer and calls `transport.abort`). It has no stall that freezes
bytes in place, and no restore that reopens the listen port. The harness uses
its own forwarder for those two. A stall holds each chunk until `unstall()`; it
does not sleep and then deliver. `fault_proxy.py` is unchanged.

## The two kinds of fallback

Connection time: the relay has not started. The preferred path is tried for
real and refused, then the other one is tried and used. No driving happens.
These scenarios should keep service:

- primary down before connect
- fallback down before connect

Midstream: a session is already open, and a nonzero simulated command is held.
An established stream cannot change upstream. The cut kills that socket, the
motors stop, and the gap is reported. A new relay and a new session on the
surviving path can restore viewing. The held command is not part of that. These
scenarios should stop, then (where a path remains) come back only as a new
session:

- primary loss during HTTP: a `/camera.jpg` request is in flight when the path dies
- primary loss during H.264: `/ws/video` is open and has delivered a fragment when the path dies. Missing ffmpeg leaves this row `passed: null` and out of the overall result. It is not a pass.
- primary loss during control: the control socket is the one cut
- stalled primary: heartbeats keep going out; the frozen return path is what expires the lease. The clock stops when `/control/status` first shows PWM 0, and that has to land inside the 250 ms lease plus one status poll. The same run without the stall keeps moving
- both paths lost (stays stopped; the gap is reported as ongoing, not as the time the harness waited)
- recovery of the primary

## What a stop must look like

The 250 ms permit lease is `ControlState.LEASE`. The 500 ms view guard is a
bare number in `jetson/control/state.py` (`0 <= video_age <= 500`) and the same
number in `quest/src/rover.ts` (`video_fresh: age < 500`). The harness reads
both. It refuses to run if either has moved.

The recovery scenario also sends one press with `video_age_ms` one past that
number, after an explicit release. The production relay rewrites that press
into a release before forwarding it, so the Jetson answers `deadman_released`
and PWM stays 0. If the press still arrived, the Jetson would answer
`video_stale` and hold the latch. Either is the rejection. An `active` answer
with nonzero PWM fails the scenario. The same press is covered by the unit
tests against the simulated server.

After a required stop the report must show PWM `0/0` and `requires_release:
true`. A cut stops with `controller_disconnected`. A stall stops because the
next permit never arrives, so the lease expires.

The qualification run never resends a held command. After a cut it opens a
new session, checks that session opened latched, and sends one new press with
no release. That press must be answered `release_required` on its own seq,
inside the lease. Waiting 300 ms and reading PWM 0 would pass even if the
server had accepted it. Motion resumes only after an explicit ordered release
and a new press, and only in the recovery scenario, on a simulated server that
has reported `simulated: true`.

The unit tests do resend the held bytes at the simulated server, and only
there, to prove the server rejects them. The qualification run does not.

- `inject_replay_attack` resends the exact held bytes on the same session (same seq, same permit). The reply for that seq must reject it, inside the 250 ms lease. A later poll of PWM 0 is not this check.
- `inject_old_command_attack` aims those bytes at a new session. The reply must be `expired_permit`. `release_required` is the latch a new session already opens with, so seeing only that means the old permit was never judged. That check goes red.

A new session is already latched, so refusing a fresh press does not by
itself prove the server remembered the old permit. The same-session probe is
the one that does. The new-session probe covers the other case. Neither one
runs inside the qualification scenarios, because those scenarios must not
resend motion bytes.

## The report

`scope` is `simulated` or, with `--live-origin`, a separate `live_read_only`
block. Per scenario: active path, interruption seconds, control RTT p50/p95,
video age, last output PWM, stop reason, explicit-release behavior, pass/fail
and why.

A midstream gap stops when the new session's first status arrives. The RTT
samples and the camera fetch happen after that, so they are not part of the
gap. Connection-time gaps are the measured refusal time. The configured cap
sits beside them as `probe_timeout_s` and is not the gap. A loss that never
ends (`both_paths_lost`) reports `interruption_s: null`, shown as "ongoing".
A stall or a total loss has no RTT and no frame age; those fields say
"not measurable", not a bare null. A skipped H.264 row is `passed: null`
and is left out of the overall pass.

RTT p50/p95 always carry a sample count. Recovery takes the drive echo plus
two stop-only heartbeats, so that row is three samples, not one sample
printed twice. The markdown puts the count in the RTT column as `n=`.

The summary says which single-path failures keep service and which correctly
stop. The markdown names the live origin when one was probed, and says live
mode was not claimed only when it was not. The live probe strips `permit` and
`session_id` whether or not the origin says it is simulated.

`--live-origin URL` only GETs `/health` and `/control/status`. It never opens
`/ws/control` and never requests motion. A cert failure, a timeout, or a down
origin is recorded in the `live` block and the simulated scenarios still run.
A self-signed Jetson will fail the default TLS check; that is reported, not
papered over by turning verification off. Leave the flag off unless you mean it.

## What this does not prove

Phone Tailscale, the rover phone at `100.97.94.113:8081`, the Jetson fallback at
`100.103.169.40:8081`, the Mac's `127.0.0.1:18443` USB forward, motor direction,
power under load, and a real disconnect stop. Those stay hardware work.
