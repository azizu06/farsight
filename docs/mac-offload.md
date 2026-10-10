# Mac rendering and Quest delivery

The Mac renders the measured map with shaded mesh materials, lighting and small
points, then encodes H.264. Quest decodes the picture and handles tracking/input.
The current streamed picture is **flat, not stereo**. This document does not
claim a tested Quest deployment or million-point reconstruction.

## Prepare before the offline rehearsal

```sh
python -m pip install -r tools/mac_render/requirements.txt
npm --prefix quest ci
npx --prefix quest playwright install chromium
npm --prefix quest run build
```

The renderer reuses the frontend's cached Chromium when Python Playwright's
browser is absent. `--browser /absolute/path/to/Chrome` selects an existing
browser explicitly. Startup downloads nothing. FFmpeg must be installed;
VideoToolbox is selected on a supported Mac, with an explicit libx264 option.

## Phone route on the operator Mac

Keep the phone USB helper and geometry assembler running as described in
[the demo checklist](demo-checklist.md). Start the relay with the separate
loopback renderer configured, then start the renderer in another terminal:

```sh
python -m tools.demo --upstream http://127.0.0.1:18443 \
  --geometry-upstream http://127.0.0.1:8484 \
  --render-upstream http://127.0.0.1:8090 --quest
python -m tools.mac_render --upstream http://127.0.0.1:8080 --port 8090
```

`http://localhost:8080/macview.html` is the standalone flat viewer. Both frame
and pose sockets use the same origin; `?mac=http://HOST:PORT` changes both.
The existing USB reverse on 8080 carries the local render routes. Keep a single
stream consumer open; simultaneous consumers cannot split dependent MP4 fragments.
For the same-page AUTO adapter, follow the current Quest instructions and
`?macview=1`; keep one controlling page open. Target restarts remain with the
operator during a break between tests.

An isolated simulation is available with no physical motors:

```sh
python -m tools.mac_render --sim-world --port 8090
```

## What changed

The renderer reads `/map/status`, including the merged phone-geometry source,
and preserves identity headers on binary map responses. It clears old capture
origins, rejects mismatched snapshots, retains unchanged revisions and serializes
bounded refreshes. A capped mesh alternates with the measured point snapshot.
The visible grid is a display guide, excluded from measured geometry counts.

Each encoded image is rendered and copied with its map/pose metadata in one
browser JavaScript turn. MP4 sample ordinals select their corresponding captured
input records; a newer unrelated capture cannot make an old fragment look fresh.
Fragment age is recomputed at send time using the Mac monotonic clock. Encoder
output flushes packets and avoids decoder frame-thread buffering. These are
render timing bounds, not physical camera age or headset pose-to-photon evidence.

Pose freshness combines server receipt age with browser-local elapsed time.
It never subtracts Quest/Python/browser monotonic clock origins. Invalid,
untracked or expired poses clear the ray and echo; last camera position may be
retained. Pose traffic is bounded and latest-only. Stop cancels retries and
closes both viewer sockets.

## Functional checks

```sh
python -m unittest tools.tests.test_mac_render tools.tests.test_mac_render_integration
node tools/tests/mac_render_page.browser.mjs
python -m tools.qualify_macview --origin http://127.0.0.1:8090 \
  --segments 12 --output /tmp/macview-stream.json
```

The stream checker verifies transport/MP4 envelopes. The browser checker in
`tools/qualify_macview_browser.mjs` additionally verifies actual video decode.
Neither establishes headset performance, stereo, actual room detail or upload
capacity. Record served revision, selected encoder, measured data counts and
real Quest playback before claiming that the operator rig is ready.
