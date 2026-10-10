# Computer-side Tailscale fallback and hardware rehearsal

The production demo's primary route is the [phone-owned cellular relay](phone-relay.md):
Quest→Mac over USB/ADB reverse, Mac→operator iPhone over USB `iproxy`,
operator phone→rover phone over cellular/Tailscale, and rover phone→Jetson over
local USB. See [demo-connectivity.md](demo-connectivity.md) for the network
contract.

**The detailed computer-side Tailscale/USB-tether procedure below is a separate
fallback only. Do not use it as the primary demo setup, and do not treat its
qualification as evidence for the phone-owned route.** The fallback's operator
USB tether has been intermittent. Neither route may use venue Wi-Fi as a silent
substitute.

The operator relay serves the built Quest app on `127.0.0.1:8080`; Quest reaches
it using ADB reverse over its USB cable. No physical hardware was connected
during the software-only validation below; USB pairing, phone routing, Quest
display and physical stop timing remain hardware acceptance gates.

No physical hardware was connected during software validation. Local simulated
mapping, camera frames, and rover telemetry are explicitly labelled simulated.
USB pairing, cellular routing, Quest WebXR, video latency, and real motor stop
timing remain hardware acceptance gates.

## Install and bundle while internet is available

From the repository root, create an environment and install dependencies:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r tools/requirements.txt -r jetson/mapping/requirements.txt
npm --prefix quest ci
npm --prefix quest run build
```

Install Android Platform Tools (`adb`), authenticate Tailscale on the MacBook and
Jetson, and enable Quest Developer Mode before judging. The launcher checks for
these tools but never installs packages, runs sudo, or changes network settings.
Install FFmpeg before trying the optional H.264 simulator. Use the production
Jetson requirements for its serial/camera service as documented separately.

All frontend assets are bundled under `quest/dist`; serving them does not need
an internet connection. This does not make remote cellular driving offline.

## Run a complete local software rehearsal now

```sh
python -m tools.demo --simulate --check
python -m tools.demo --simulate
```

Open `http://localhost:8080`. With FFmpeg installed, exercise compressed video:

```sh
python -m tools.demo --simulate --h264
```

For a connected Quest, add `--quest`; when several ADB devices are attached,
also add `--adb-serial QUEST_SERIAL`. Check and build in one launch using
`--build` if the installed frontend dependencies are available. Ctrl-C stops
both local processes. Startup fails if a selected port is already in use.

Tests use a subprocess relay and actual TCP connections to a simulated upstream:

```sh
python -m unittest tools.tests.test_operator_relay -v
python -m tools.qualify_link --seconds 30 --output /tmp/farsight-link.json
```

The qualifier sends **only `deadman: false`**. It measures acknowledgement RTT
while consuming video and reports observed bitrate. It measures H.264 when the
health response reports it ready, and MJPEG otherwise; select `--video-mode h264`
to explicitly qualify the compressed path. Its passing transport checks
do not verify camera-to-display delay, spare upload capacity, or physical stop
timing. It never claims the complete demo is qualified.

Only one control socket can own the rover. Close the Quest/browser page before
running the qualifier, then reopen it afterward. A second controller is rejected
rather than taking control away from the current operator.

## Fallback hardware connection: computer-side Tailscale over USB tethering

1. Keep the rover wheels raised. Connect the Uno to the Jetson by USB and the
   camera locally. Start the documented rover service with real serial selected;
   confirm the health response identifies a real rover rather than simulation.
2. Connect the rover phone to the Jetson by a data cable. Unlock it, approve
   trust/pairing, enable Personal Hotspot, and verify the USB interface and route
   using `ip -br address` and `ip route`. Check `modinfo ipheth` if an iPhone USB
   interface never appears. Personal Hotspot does not supply phone camera frames.
3. Connect the operator phone to the MacBook using USB, approve Trust, and enable
   Personal Hotspot. In macOS Network settings, confirm the phone USB service is
   connected. Disable Wi-Fi on the MacBook and Jetson. Confirm internet traffic
   works through the phone USB links.
   Unplug the **MacBook-to-Jetson commissioning cable** for this test: Tailscale
   can select that direct USB path even when the Mac's default route is the
   relay phone. Keep camera phone → Jetson, relay phone → MacBook and Quest →
   MacBook connected. On the operator Mac, record the read-only route check:

   ```sh
   python -m tools.qualify_route --output /tmp/farsight-route.json
   ```

   It checks the iPhone USB default route, Wi-Fi being off and absence of an
   active rover USB IPv4 path. A failure or missing evidence must be resolved
   before claiming cellular qualification. This only verifies Mac routing
   conditions; separately check the Jetson's phone route, real link performance,
   headset rendering and motor stop behavior. It changes no network settings
   and does not open a control connection, so the viewer can stay open.
4. Run `tailscale status` on both computers and `tailscale ping JETSON_PEER` on
   the MacBook. Record direct versus relayed connectivity. Restrict access to the
   rover through the tailnet policy. The software does not configure that policy.
5. Start the relay with the actual Jetson origin:

   ```sh
   python -m tools.demo --upstream http://JETSON_TAILNET_IP:8443 --quest
   ```

   The board profile is plain HTTP with TLS omitted ([jetson-runtime-results.md](jetson-runtime-results.md)),
   so select `http://` explicitly; it is never an automatic TLS fallback. If the service was installed
   with TLS instead, use `https://JETSON_TAILNET_NAME:8443`: HTTPS verifies certificates, a private CA
   needs `--ca-file /path/to/ca.pem`, and a bare IP fails validation if it is absent from the certificate.
6. Unlock the Quest, approve USB debugging, and verify `adb devices`. The launcher
   configures `adb reverse tcp:8080 tcp:8080`. Open `http://localhost:8080` in Quest
   Browser. Verify immersive VR and the secure-context indicator on the headset;
   desktop success does not prove Quest compatibility. Reapply ADB reverse after
   reconnecting USB or restarting the headset.
7. Close the Quest page and run the qualifier from another MacBook terminal; it
   consumes video itself. Reopen the Quest page afterward. Look for
   acknowledgement p95 comfortably below the 250 ms lease; its default 200 ms
   transport threshold leaves some margin but is not proof of reliable driving.
   Measure real displayed video age using a physical timer/camera. Reject the
   live driving path if it repeatedly stops under ordinary cellular load or if
   displayed frames exceed 500 ms age.

## Failure rehearsal before placing the rover on the floor

- Run with inference, video, telemetry, and controls together for 15 minutes.
  Record video age, observed bitrate, control RTT, inference rate, CPU load, and
  power/temperature behavior. Demonstrate at least twice the measured outbound
  bitrate in available upload capacity separately.
- Disconnect Quest USB, each phone, and the camera in turn. Check the Jetson
  stops on its 250 ms control lease and the Uno stops on its independent 300 ms
  motor watchdog. The real stop times need measurement, not visual guesses.
- Saturate or delay the link. Stale video must disable motion. The server rejects
  expired permits; the relay never reconnects or replays commands. A slow video
  consumer gets a closed video connection instead of an ever-growing queue.
- Reconnect. Keep the deadman released until fresh video and a new control session
  are visible; motion must require a new press. Use another route only if that
  exact route has already passed the same end-to-end and fault-recovery checks;
  otherwise switch to labeled replay or simulation with motors disabled.
- Cold boot and repeat with venue Wi-Fi disabled. If both radio paths fail,
  demonstrate the clearly labelled simulation or retained recording with physical
  motors disabled. Live untethered video requires a functioning wireless path.

## Relay behavior

The relay has independent HTTP/video and control connection pools. It forwards
streaming chunks without collecting a full video response, preserves camera frame
metadata, removes hop-by-hop headers, and uses short send deadlines. Control
messages have no application queue and no automatic upstream reconnection. A
reported stale view normally turns an attempted drive command into a stop; the Jetson's
own permits and watchdog remain the safety authority.

Only configured-upstream absolute URLs are rewritten to local paths. Redirects
to another origin are rejected. Foreign browser origins and non-local Host headers
are rejected; the listener is always bound to `127.0.0.1`. `/relay/health` checks
the local operator process independently of `/health`, which checks the rover.

For repeated control disconnects, read `GET /relay/health` and inspect
`control_closures`. It records single-route control connection counts and at
most eight endings, with UTC time, duration, the observed side/reason and
numeric close codes captured before relay cleanup. It stores no control
payloads. Correlate these times with Jetson serial exceptions and kernel USB
reset/detach logs. This observation identifies where the relay noticed loss;
it does not prove whether USB power, cellular service or another component
caused it. Bonded control is explicitly excluded and uses its separate
`transport` report. These diagnostics do not change stop or reconnect rules.

## Explicit single-route relay demo mode

The relay defaults to zeroing a held-deadman command whose reported displayed
video is not fresh. An explicit `--demo-mode` opts out of only that relay
conversion:

```sh
python -m tools.demo --upstream http://127.0.0.1:18443 --demo-mode --quest
```

The same flag is available on `tools.operator_relay`. It forwards the real
`video_fresh` and `video_age_ms` values without replacing them or claiming a
fresh camera. `GET /relay/health` exposes `demo_mode: true` only when selected;
there is no environment-variable fallback. It does not enable the Jetson
server's separate demo mode or change the viewer's freshness gates. The
server continues to apply its own configured validation and stop behavior.

This option supports single-route control only: combining it with
`--bonded-upstream` fails before startup. Malformed/non-object JSON and binary
control still close with 1008, control sends retain the 100 ms cap, and
disconnects close the upstream without replay. Frame/pose and geometry lanes
retain their existing routing, limits and behavior. See the exact scope in
[the control interface](interfaces.md#optional-demo-guards---demo-mode-farsight_demo_mode1).

## Optional local Mac render transport

The render transport is disabled by default. If the Mac renderer is already
running on loopback port 8090, opt in when launching the relay:

```sh
python -m tools.demo --upstream http://127.0.0.1:18443 \
  --render-upstream http://127.0.0.1:8090 --quest
```

For direct relay startup, the same `--render-upstream` flag is available on
`python -m tools.operator_relay`. Neither command launches the renderer.
The origin must be credential-free loopback HTTP(S), without a path, query or
fragment. A loopback Jetson route cannot share its port, including aliases such
as `localhost` and `127.0.0.1`. HTTPS retains certificate verification.

Only `/ws/macview` and `/ws/macview-pose` use this origin. Without configuration,
both endpoints return 503 and are never forwarded to Jetson. Frame/pose text
and binary messages are opaque and retain their exact bytes/order: the relay
does not rewrite `video_init`, `next`, `video_segment` or version-1 pose fields.
The renderer owns that protocol, capture timestamps, stereo rendering and
head/ray interpretation.

The render session has a separate connection pool and admits at most four
sockets, including connections still opening. Additional attempts fail instead
of waiting in a connection queue. Frame messages are bounded by 8 MiB with a
500 ms maximum send deadline; pose messages use 16 KiB and 100 ms. A closure or
stalled send tears down that pair. The relay has no render-message replay queue
or automatic reconnect. Existing browser Origin/Host checks and rover control,
camera-age and lease guards remain in effect.

`GET /relay/health` reports `render.configured`, reserved endpoint names and
`transport_only: true`. Configuration does not prove a running renderer, a
fresh view, stereo correctness or Quest qualification. The focused tests use
real loopback sockets with synthetic opaque records, not an actual renderer:

```sh
python -m unittest tools.tests.test_render_relay
```
