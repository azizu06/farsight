# Desktop reconstruction lab

Run the read-only viewer beside the existing rig relay. It binds to localhost
8086; no relay restart or Quest change is required. It has no control or pose
route, never takes a drive lease, and discards Quest telemetry locally rather
than overwriting headset metrics.

```sh
node quest/node_modules/vite/bin/vite.js build --config tools/dense_viewer/vite.config.mjs
python -m tools.dense_viewer --port 8086
# Open http://127.0.0.1:8086/
node tools/observe_dense_viewer.mjs --out .cache/dense-lab --rounds 3 --interval 10
```

Use orbit/pan/zoom, WASD/QE flight, or Front/Side/Above. The local mode imports
Quest's actual `DenseMapView`: same binary decoder, per-chunk buffers, LOD,
resets and round-point shader. The lab overrides only splat diameter/max pixel
size. Defaults remain 12 mm, 1.25 px floor; compare 24/40/60 mm in the lab before
changing production. Capture is frozen by default during a sweep so every
setting uses the same measured samples. `--live` instead measures ongoing input.
`--mode raw` selects the optional raw lane below. JSONL, CSV, PNGs and a request
ledger are written under the selected `.cache` directory. Source/count labels
remain visible in the PNGs. All automation uses an isolated headless Chrome
with SwiftShader; its FPS is desktop software-renderer evidence, not Quest FPS.

The lab raw proxy is **off by default**: selecting raw mode returns 503 without
contacting the rover. `python -m tools.dense_viewer --raw` is reserved for an
approved, measured link-budget window or an offline fixture upstream. Raw pulls
were disabled during the live AUTO run after uncompressed frame traffic was
identified as competing with camera delivery; compression alone does not prove
enough bandwidth. Keep the running cellular lane off until its owner approves.

Coverage is the fraction of canvas pixels differing from background RGB(8,12,18)
by a summed channel difference >24. Dark surfaces may be undercounted. Object
coverage uses the foreground bounding box. Hole count is enclosed background
components of >=4 pixels within that box, with 4-neighbor connectivity. Open
unobserved regions are excluded from hole count; read coverage and images too.
These are screen-space appearance measures, never room completeness. The first
black PNG set `.cache/027-first` exposed a hidden-video CSS overlay and is invalid
as visual evidence; `.cache/027-visible` is the corrected real measured sweep.

The clean encoded Mac-render preview is owned by Sai's desktop agent
(`browser-entry.ts` / `desktop-mac.ts`); this lab does not open any stream or pose
socket. Compare its encoded preview separately once supplied. Final architecture
is Mac rendering streamed to Quest; direct dense rendering here is diagnostic.
A stereo producer without a valid headset eye pair can legitimately report not
ready; never synthesize a pair to make the indicator green.

## Optional same-frame RGB-D lane

The Python rig requires an optional raw RGB-D GET mount. Its depth JPEG
is a visualization, not metric depth, and cannot substitute for raw depth.
The optional `jetson.mapping.raw_capture.mount(app)` adds `GET /phone/frame.bin`
after the service owner creates the mapping app. Set `FARSIGHT_RAW_CAPTURE=1` in the service environment to opt in; it is
**off by default**. Coordinate the mount/deployment/restart with the
rig owner at idle; do not restart the Jetson just to launch the desktop viewer.
The existing relay `/phone/` prefix forwards that GET without a relay edit.

The endpoint retains one already validated frame by reference, serializes only
on an HTTP reader thread, and rejects old socket generations, resets, paused
capture, tracking loss, disconnect and >2s-old observations. Wire layout is the
existing phone packet: u32LE JSON byte length, JSON, native JPEG, float32LE metric
depth, uint8 confidence. It adds `map_generation` for explicit mapper clears.
`X-Capture-Key` includes session/epoch/generation/frame; pass `?after=KEY` for a
204 when unchanged. `X-Capture-Age-Ms` reports producer-delay-adjusted receipt age
rather than verified sensor latency.

The desktop raw mode polls at most 2 Hz, projects every valid native pixel at
its own ARKit pose in a worker, associates measured positions at 1 cm (no
voxel-center snapping), keeps up to 2M points, and textures triangles only
between adjacent confident depth samples with continuous depth. Recent patches
are bounded by 12 images, 500k vertices and 1M triangles. Unsegmented samples and
patches use a conservative 30s rolling lifetime; moving people are not permanent
room geometry. A frozen comparison is explicitly historical.

This first port follows the calibrated preview/worker/association/topology
approach in [Pip Scout's viewer](https://github.com/azizu06/pip-scout/blob/main/dashboard/VIEWER.md)
and [persistent scan](https://github.com/azizu06/pip-scout/blob/main/dashboard/PERSISTENT_SCAN.md).
It does **not** port their planar compression or two-view moving-silhouette
retirement, and does not claim a permanent million-triangle reconstructed room.
Raw GPU uploads reuse preallocated buffers but send a full retained point range
per accepted frame in the lab's history mode; qualify this cost on a large cloud before increasing
capture rate. This is an optional desktop prototype, separate from navigation.

The Mac adapter projects every valid native depth pixel in **latest** point
mode and retains recent camera-textured surface patches independently. It does
not associate, rebuild or transfer the retained point history. The core Mac
renderer already retains those points, so the adapter's duplicate point overlay
is hidden. Point buffer capacity is
the largest accepted native grid (at most 640×640), not 2M points. A 256×192
frame transfers 1,179,648 bytes of point position/color storage, independent of
scan length; mesh arrays and the texture are additional. Confidence-zero pixels
are omitted, depth discontinuities cannot be bridged, and session/generation
resets and the 2s freshness deadline still apply. Latest points expire at
2s capture age. Accepted textured patches remain for up to30s, bounded by the
same12 images,500k vertices and1M triangles as the lab. Session/epoch/generation
changes clear both. Nearby views replace the last patch; distinct views retain
their measured RGB detail after the camera turns away. Texture filtering uses
anisotropy4 for oblique floors/walls. These are recent historical observations,
not live images of an unseen moving person.

`pollInterval` defaults to500ms and is clamped to125–4000ms with one pull/worker
job in flight. The default2Hz ceiling is not a measured live rate or permission
to enable the optional stream. `surfaceHistory:false` restores a single recent
patch. Stats expose the oldest surface age and the independent30s surface
lifetime; point capacity and transfer cost stay bounded by one native grid.

Checks:

```sh
python -m unittest jetson.mapping.tests.test_raw_capture tools.tests.test_dense_viewer
node --test tools/dense_viewer/metrics.test.mjs
python - <<'PYTHON'
from pathlib import Path
from jetson.mapping.tests.test_mapping import bundle, flat_frame
Path('.cache').mkdir(exist_ok=True)
Path('.cache/raw-fixture.bin').write_bytes(bundle(flat_frame(), map_generation=1))
PYTHON
node tools/tests/dense_viewer.browser.mjs http://127.0.0.1:8086/ .cache/raw-fixture.bin
# Build the adapter with vite.layer.config.mjs first:
node tools/tests/raw_layer.browser.mjs
# Native WebGL before/after: coarse gaps and texture loss after a new viewpoint.
node tools/tests/mac_render_detail.browser.mjs --out /tmp/farsight-render-detail
```

The browser check exercises the real worker's 48 measured points, 70 supported
textured faces, positive canvas coverage and no control/pose connection. It is a
synthetic decoder/render check, not live raw-capture acceptance.

The live viewer follows measured scan bounds by default. Dragging or flying disables follow so you can inspect a fixed location; re-enable **Follow live scan** to return. Capture epoch changes reframe the new coordinate system. A healthy fresh feed can look frozen when a camera remains aimed at an earlier scan location.

The **Open Mac-rendered preview** link opens `http://localhost:8080/?view=map`, the teammate-owned encoded Mac output viewer. The lab does not open an additional stream until that link is used.

For native camera textures open `http://127.0.0.1:8086/?mode=raw`. **Camera-textured surfaces** and **Measured points** can be compared independently. Supported depth triangles draw first with a small depth offset; later points pass the depth test only where they are uncovered. Samples stay retained and become visible again when a texture patch expires. This suppresses coplanar dots without deleting measured fallback geometry.

Compare the same captured samples with and without textures:

```sh
node tools/observe_dense_viewer.mjs --mode raw --compare-surfaces --sizes 0.012 --out .cache/raw-compare
```

Open `.cache/raw-compare/index.html` for a dark screenshot review page with paired
coverage, source, frame and capture identity. `report.json` includes the same
pairs and the read-only request audit, including when a run fails. Comparisons
require frozen raw mode and reject `--live`; every row must retain the same
accepted frame, identity and geometry. Freezing stops new pulls before draining
the in-flight frame. The raw view starts at the displayed 12 mm world size and
uses a 1.25 pixel floor for distant measured samples.

The owner-mounted Mac adapter is `tools/dense_viewer/mac-layer.js`. Build it with `node quest/node_modules/vite/bin/vite.js build --config tools/dense_viewer/vite.layer.config.mjs`. The resulting `.cache/dense-layer-dist/raw-texture-layer.mjs` exports `rawTextureLayer({endpoint:'/phone/frame.bin',enabled:false,pollInterval:500,surfaceHistory:true})`; mount its adjacent `assets/` worker directory at the same origin, import it and pass the result to `window.__macRegisterLayer`. It shares the renderer's existing Three.js, uses `ctx.root` and `ctx.requestFrame()`, and exposes `layer.stats()`. Networking is default-off; only the runtime owner enables it in a qualified window with `layer.setEnabled(true)`. Disabling hides it and aborts an active HTTP pull. Stats expose mode, native point capacity, transferred point bytes, patch count and cadence. The production renderer includes these stats in capture metadata under `surfaceDisplay.raw`.

`layer.captureOnce()` enables display and performs one explicit GET with polling
off. The accepted texture stays visible with its normal 30s expiry; later render
ticks start no new pulls. This supports a one-shot idle qualification without
starting a raw stream. `layer.setPolling(false)` also pauses future pulls while
keeping already accepted surfaces visible and expiring; stats expose
`raw_polling`. Re-enable continuous pulls only in the owner's qualified window.

The production Mac renderer draws measured surfaces before fallback points.
Its coarse splats follow the map's advertised voxel spacing (1.8×voxel size,
72mm at4cm cells, corrected for the camera projection/FOV); the1cm dense lane
uses a separate35mm material, matching the Pip Scout point footprint. Both
retain native linear RGB without tone mapping, use a2px floor and64px ceiling,
and upload no geometry when only the camera moves. This
display footprint does not add measured points or fill missing depth triangles.

Raw exports negotiate br/gzip and retain only the newest encoded packet variants. The relay owner must include `/phone/frame.bin` in its compression pass-through paths before this helps the tailnet hop. A recorded 270,259-byte physical packet compressed to 70,557 bytes with Brotli q4 in 3.1 ms on this Mac; that is a local compression measurement, not Jetson CPU or live link proof. Initial live uncompressed reads sometimes took 2.8 s and were rejected by the unchanged 2 s capture freshness limit. No service is restarted by the lab or adapter.

A same-view recorded physical packet comparison (frame 1230, 2026-10-11 01:32 UTC, 640×480 JPEG / 256×192 native depth) kept 6,565 measured 1 cm cells and 83,709 supported triangles. Front-view canvas coverage rose from 0.78% with points alone to 2.05% with textured surfaces; side/above remained sparse. This is recorded desktop SwiftShader evidence, not live delivery, room completeness or headset FPS. Its source was visibly labelled RECORDED IPHONE LIDAR.
