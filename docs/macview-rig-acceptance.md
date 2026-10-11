# Mac render → actual Quest: operator handoff

Owner: Aziz/Firstmate runs the operator Mac, Quest, and H.264 viewer; Devin and
Dechante coordinate renderer visuals/encoding; Codex owns joined transport
integration and renderer protocol diagnostics. Confirm current file handoffs on #24. Track results on [#120](https://github.com/azizu06/farsight/issues/120)
and coordinate on [#24](https://github.com/azizu06/farsight/issues/24).
This checklist does not itself qualify hardware or authorize a second control session.

## 1. Pin the exact running candidate

Firstmate must name the combined renderer/API and same-page Quest SHAs before
deployment. The paired renderer is implemented by `85c7253`, with software
raster/codec evidence in `docs/qualification/mac-render-stereo-20261010` and
continuous-player evidence in `docs/qualification/mac-render-sbs-stream-20261010`.
Those checks do not qualify the actual Quest or the current served bundle. Pin
the latest integrated controls, HUD and offload repairs as well.
Use the operator's existing checkout/runtime and preserve its dirty files. The
commands assume an activated Python environment. For the standard repository
setup, activate it first; use Firstmate's selected equivalent if it differs:

```sh
source .venv/bin/activate
```

```sh
run_dir="$PWD/docs/qualification/macview-rig-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$run_dir"
git rev-parse HEAD > "$run_dir/checkout-sha.txt"
git status --short > "$run_dir/dirty-state.txt"
git log -1 --format='%H %cI %s' > "$run_dir/checkout-log.txt"
adb devices -l > "$run_dir/adb-devices.txt"
adb reverse --list > "$run_dir/adb-reverse-before.txt"
curl --fail --max-time 3 http://127.0.0.1:8080/relay/health > "$run_dir/relay-health.json"
curl --fail --max-time 3 http://127.0.0.1:8091/health > "$run_dir/render-health.json"
curl --fail --max-time 3 http://127.0.0.1:8080/map/status > "$run_dir/map-status-before.json"
```

Return the exact active relay/render commands (redact credentials), upstream
map source, Quest URL, and the SHA/time that built the *served* `quest/dist`.
Checkout HEAD alone does not prove which files a running process/browser loaded.
Keep SHA/build provenance in the evidence directory. A missing renderer, inactive
`render.configured`, absent Quest, or unpublished embed is a named blocker.

## 2. Launch only through the existing deployment owner

If the services already run, continue to step 3. Firstmate owns any needed
reconfiguration/restart. Do not start competing listeners or copy these example
commands over existing geometry, TLS, bonding or demo-mode flags.

Dependencies in the selected Python runtime: `aiohttp`, Python `playwright` with
Chromium, and FFmpeg with `h264_videotoolbox`. Check before launching:

```sh
python -c 'import aiohttp; import playwright.async_api'
ffmpeg -hide_banner -encoders | rg 'h264_videotoolbox'
python -m tools.mac_render --help
python -m tools.dense_map --help
```

After Firstmate combines the candidate with the current served build:

```sh
npm --prefix quest run build
```

For a fresh owner-approved launch, use two terminals. Substitute the real source
and preserve the existing relay flags. The renderer reads the merged map through
8080; it listens on 8091. The relay routes only the two render sockets to 8091.

```sh
# Terminal 1: dense relay; retain the owner's selected runtime flags.
python -m tools.dense_map --upstream "$jetson_origin" --port 8080 \
  --render-upstream http://127.0.0.1:8091

# Terminal 2: actual mapped room, not --sim-world.
python -m tools.mac_render --upstream http://127.0.0.1:8080 --port 8091 \
  --stereo sbs-left-right --width 1920 --height 1080 --fps 30 --hardware videotoolbox
```

`jetson_origin` must be the actual existing upstream, set by Firstmate; it is not
an example address to paste unchanged. `tools.dense_map` mounts the Mac dense
accumulator directly; do not add `--dense-map` to that command (that option belongs
to `tools.demo`). Preserve the selected `--demo-mode`, TLS, bonding and geometry
flags. The current rig uses 8091 because 8090 is occupied; verify the actual listener
before changing ports. A reachable upstream `/health`, successful `/dense/status`,
and positive renderer `/health` counters under `dense` must agree: `retained_cells`,
`gpu_uploaded_cells`, and `gpu_drawn_cells`. A ready encoder or coarse snapshot
count alone does not prove dense ingestion. Missing Python/browser
dependencies are setup failures; `--no-browser` is not a working visual stream.

## 3. Connect the paired headset before receive-only transport checks

The renderer supports multiple receive-only viewers with independent keyframe-safe
queues, but only one visual pose writer. Run transport checks before the timed
single-viewer baseline, while the headset supplies its own paired pose. A
receive-only checker does not move the view; a browser or `--probe-pose` sender
does. Density GET checks can run during a session.

A packed producer requires a fresh ordered left/right eye pair. Before the
headset enters AUTO, `ready:false` can mean it is waiting for that pair, rather
than a broken encoder. Keep exactly one existing Quest Browser control page at
`http://localhost:8080/?macview=1&stereo=1&diag=1`, enter its immersive session,
and select AUTO using A. The actual XR pose sender supplies both eyes; do not
invent a desktop pair or add another pose writer. Only reverse 8080 is needed.
Confirm `configured_layout:sbs-left-right`, `stereo:true`, advancing pair/frame
identity and the selected source on the running renderer.

```sh
adb reverse tcp:8080 tcp:8080
adb reverse --list > "$run_dir/adb-reverse-after.txt"
# With the headset's paired pose writer active, observe one hop at a time.
python -m tools.qualify_macview --origin http://127.0.0.1:8091 \
  --layout sbs-left-right --segments 30 --timeout 20 --output "$run_dir/direct-stream.json"
python -m tools.qualify_macview --origin http://127.0.0.1:8080 \
  --layout sbs-left-right --segments 30 --timeout 20 --output "$run_dir/relay-stream.json"
python -m tools.inspect_map_density --operator http://127.0.0.1:8080 \
  --geometry http://127.0.0.1:8484 --renderer http://127.0.0.1:8091 \
  --timeout 5 > "$run_dir/density.json"
```

These extra receive-only streams add encode/network/USB load; record that
condition, then close them before measuring the normal single-viewer headset
baseline. They do not create a control session or send a pose. SBS transport
checks reject the mono `--probe-pose` option. The configured total dimensions
must divide into two halves; the example uses 960×1080 per eye. If the owner
selects 1280×720 total, record its 640×720 per-eye dimensions and qualify visual
quality at that resolution rather than describing it as 1080p per eye.

The checker is receive-only by default. Do **not** use `--probe-pose` while someone
uses the headset: it changes the render camera. Do not run the older pose-writing
benchmark against an occupied render session. A passing checker proves advancing
fragment IDs, timing metadata and MP4 envelopes; it does not decode video or prove
headset FPS, source truth, stereo, or pose-to-photon latency.

The density snapshot reports only the selected endpoints' actual fields. Unknown
counts stay null; retained mesh vertices and repeated observations are distinct
from unique measured points. Older Python deployments fall back from operator
`/map/status` to `/health` only on HTTP 404, recording both attempts and the actual
evidence URL. Other failures remain failures. A failed hop still writes JSON
with its error.

For a separate **mono** checkpoint only, run the standalone browser helper
against an explicitly mono producer. The standalone page rejects packed stereo;
do not point this command at the SBS runtime. The helper records decoding, errors,
seeking events, graphics backend and a screenshot:

```sh
node tools/qualify_macview_browser.mjs \
  --url 'http://127.0.0.1:8080/macview.html' --out "$run_dir/desktop-browser"
```

This page sends its normal desktop visual pose, so run it with an isolated
renderer rather than alongside the headset's pose sender. Its video-presentation
cadence is desktop evidence, not XR FPS or pose-to-photon latency. The default
minimum callback rate is only a decode sanity check: compare measured cadence and
stalls with the configured stream rate before claiming smooth playback. A private
seek-suppression experiment is not proof of the deployed viewer.

Keep the same paired Quest page with `?macview=1&stereo=1` for the actual
headset acceptance. Add `&diag=1` for measurement, then remove diagnostics for
the minimal-HUD capture. Firstmate must
confirm the served revision implements that flag; a URL is not implementation
evidence. Refresh/navigate the current page rather than opening a second control
tab. If the embed is absent, stop final integration acceptance and report it.

The isolated `http://localhost:8080/macview.html` page defaults its stream to the
relay origin. It is a **flat visual-only checkpoint**, opens no rover control,
and cannot qualify same-page A/AUTO or stereo VR. Use it only in a separate
stationary qualification session after the active control owner releases.

## 4. Return actual headset and density evidence

Use the same measured room/map and camera load for before/after. Record device,
resolution/bitrate, configured headset refresh rate, source identity, elapsed
acquisition time, and whether the rover is stationary or exploring. In a 30-second
actual immersive session capture:

- Recognizable room surfaces and texture/color detail, smaller point size, and
  retained **unique measured points** versus Mac loaded/drawn points. Request
  phone/assembler retention and the actual dense API/status from their owners;
  historical observations, native mesh vertices and synthetic points differ.
- Existing renderer-page `window.__mac` counters (`points`, `drawnPoints`, source,
  retained/freshness/map revision where present), actual point-size setting and
  running SHA. Inspect the existing renderer page; don't create another renderer
  or pose sender. A high count with an empty/incorrect room is not a quality pass.
- Actual Quest FPS/frame-time p95, decoded frame age p50/p95, advancing decoded
  frames, freeze/stutter duration and tracking behavior. Keep stream transport
  age separate from physical camera age and measured display latency.
- A headset screenshot/short recording of the actual view with diagnostics, then
  the default minimal HUD. If ADB capture is black or omits the immersive layer,
  retain a Quest cast/headset recording instead and state its source.

```sh
adb exec-out screencap -p > "$run_dir/quest-view.png"
curl --fail --max-time 3 http://127.0.0.1:8080/map/status > "$run_dir/map-status-after.json"
```

## 5. Same-page control and stereo completion gates

Aziz runs controls on the safely prepared real rig. Record PASS/FAIL and video or
control-log evidence for default MANUAL direct right-stick driving at full
configured motor demand, centered-stick stopping, A → AUTO, virtual movement
without a physical stick override, and A → MANUAL cancellation. The latest
requested two-mode interface uses the joystick as motion intent rather than an
extra trigger requirement; qualify the actual served implementation. Confirm one
active `/ws/control` owner throughout, separately from cumulative reconnect count.
Record B/STOP, tracking loss, stale-camera behavior, disconnect and recovery under
the selected demo profile using the rig owner’s procedure; keep the applied
settings visible and do not infer their values from defaults.

Quest should perform tracking/input, decoding and minimal presentation. Geometry
accumulation/decompression and dense rendering belong on Mac; Jetson retains
navigation/inference and motor leases. Confirm AUTO stops heavy local geometry
decoding/draws on Quest, rather than rendering a hidden dense cloud behind video.

A stream declaring `stereo:false` is flat even inside immersive WebXR. Full VR
requires actual per-eye images/projection/view aligned with head pose, virtual
locomotion, floor/recenter and session/map identity, plus real-headset visual and
performance evidence. Do not mark #120 complete from standalone playback.

Post the evidence directory/artifact links, candidate + served SHAs, each gate's
PASS/FAIL, and the next named owner/blocker to #120. Link the result on #24 so
integration can act immediately. Missing hardware measurements remain unverified.
