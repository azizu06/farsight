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

The deployed Python rig initially has no raw RGB-D GET endpoint. Its depth JPEG
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
per accepted frame; qualify this cost on a real large cloud before increasing
capture rate. This is an optional desktop prototype, separate from navigation.

Checks:

```sh
python -m unittest jetson.mapping.tests.test_raw_capture tools.tests.test_dense_viewer
node --test tools/dense_viewer/metrics.test.mjs
# Generate fixture using bundle(flat_frame(), map_generation=1) from
# jetson.mapping.tests.test_mapping, then:
node tools/tests/dense_viewer.browser.mjs http://127.0.0.1:8086/ .cache/raw-fixture.bin
```

The browser check exercises the real worker's 48 measured points, 70 supported
textured faces, positive canvas coverage and no control/pose connection. It is a
synthetic decoder/render check, not live raw-capture acceptance.

The live viewer follows measured scan bounds by default. Dragging or flying disables follow so you can inspect a fixed location; re-enable **Follow live scan** to return. Capture epoch changes reframe the new coordinate system. A healthy fresh feed can look frozen when a camera remains aimed at an earlier scan location.
