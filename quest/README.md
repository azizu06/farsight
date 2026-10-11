# Quest operator app

The default page opens the live rover camera. `?view=map` opens the actual Mac
renderer output in the clean desktop canvas; `?view=map&local=1` opens retained
local geometry. Requests use the page origin, so the headset stays on the MacBook's USB
relay instead of trying to reach the Jetson directly.

The Mac preview decodes `/ws/macview` directly into the canvas. It automatically
tries flat and packed side-by-side streams; for SBS it shows the actual left-eye
image at its original aspect ratio. Mouse inspection works without a headset and
owns `/ws/macview-pose`
when available, using an illustrative 64 mm desktop eye separation for SBS;
it never opens `/ws/control` or downloads geometry. An existing headset pose
owner remains authoritative, and the desktop mirrors that output. Disable
**Inspect with mouse** or use `&inspect=0` for an explicitly receive-only preview. Escape opens
preview settings, including source/counts, decoded picture age, renderer origin
and a receive-only toggle. No diagnostic text covers the picture.

Use the operator relay's `--render-upstream http://127.0.0.1:8091` for the real
renderer, or `?view=map&mac=http://localhost:8091` for a direct local connection.
`&stereo=1` pins packed stereo, `&stereo=0` pins flat; without either, the preview
negotiates by trying the two existing stream routes. A disconnected renderer
shows only the subtle waiting grid; it never substitutes a simulated scene.
Packed rendering also remains available when a connected headset is unworn:
the renderer marks its own desktop camera fallback separately, and tracked
headset output resumes when valid paired poses return. Quest rejects the
desktop fallback as tracking output; the browser can inspect its real pixels.

The local desktop map is a bare canvas for the incoming 3D points: right-drag orbits,
left-drag pans, scroll zooms, and F frames the scan. **Escape** opens settings;
**Controls** inside settings opens rover actions and source, connection and stop
details. Escape closes the controls again. Opening controls or changing views
stops motion and requires a fresh deadman press. A subtle grid gives depth context;
the floating HUD is absent. Points-only is the desktop default; headset geometry remains hybrid.

Build with `npm ci && npm run build` from `quest/`. Run `npm test` for protocol,
full-frame geometry, action-menu hit testing, and control safety checks. `npm run test:browser` runs the
production `RoverExperience` in headless Chromium (SwiftShader, simulated XR
poses and generated camera pixels, no backend) for the two-mode interface,
direct joystick driving, autonomous map roaming, tracking/permit expiry,
mode transition release and the minimal rendered cockpit;
run `npx playwright install chromium-headless-shell` once first. `npm run perf`
profiles the built app under an emulated Quest 2 (IWER) against a running
simulator; see the [first-run guide](../docs/quest-first-run.md#7-frame-budget).
Use the [operator launcher](../tools/demo.py)
for the integrated app; Vite development mode uses port 8443 as its upstream.
Do not confuse an old server already on that port with a freshly launched demo.

Full map transfers start at most once every five seconds, with one request in
flight and only when the selected geometry revision changes. Hybrid 3D view
downloads the mesh and derives its point overlay locally; points-only/2D mode
requests the point snapshot. Same-source reconnects keep applied revisions.
Opening camera settings does not download a map, and holding the deadman pauses
map transfers. Source/session, epoch or mapper generation changes clear retained
geometry. This reduces snapshot traffic while the phone-to-Mac delta lane lands.

`node tests/vr-visual-audit.mjs --after` launches its own isolated simulated
server/relay and captures the production stereo view and menu hover into `/tmp`.
For comparative upload profiling, point `PERF_ORIGIN` at a separate simulated
relay and run `node tests/perf-audit.browser.mjs`; `PERF_RETURN=1` also checks
camera return after map mode. These are desktop diagnostics, not Quest
qualification. The scoped redesign measurement is saved in
[quest-vr-redesign.json](../docs/qualification/quest-vr-redesign.json).

## Controls

- VR starts in **MANUAL**. Move the right joystick to drive and turn the rover;
  no trigger press is needed. Center it to stop. Its full-scale commands use the
  configured server motor ceiling. The left joystick does not drive the rover.
- Press **right A** to switch to **AUTONOMOUS**. The rover explores with its own
  code while the joysticks move the viewer through the reconstructed map. Left
  stick moves/strafe; right stick moves forward/back and snap-turns 30°. These
  virtual movements always send physical `v=0, w=0`; they do not override autonomy.
- Press A again for Manual. Center the joystick before driving after the switch.
  A toggles on its press edge; holding it has no secondary action. B stops and
  returns to Manual. The VR HUD contains only the mode pill and A affordance;
  telemetry, action menus, controller diagrams and auxiliary panels are absent.
  Operator telemetry and advanced controls remain on the laptop page.
- **Demo mode** (`src/demo-config.js`) is ON by default; `?demo=0` restores every
  client guard. While immersive it removes the client 500 ms camera-stale block
   rides out right-controller blips or a
  re-enumerated controller for up to 1 s, and replaces the forced trigger re-press
  after a transient stop with one client-sent release. AUTO is hands-free:
  Explore heartbeats carry deadman=true without the trigger until A or B. Hard
  stops (B, a MANUAL/AUTO switch, session start/end, viewer closed) still need a
  centered joystick before manual movement. Permits, leases and link-loss stops are unchanged, and `video_fresh`
  stays truthful; the rover's own camera checks follow its separate
  `--demo-mode` / `FARSIGHT_DEMO_MODE` (see `docs/interfaces.md`). The desktop
  page keeps every guard.
- Voice and assistant controls remain on the laptop page when the rover has the
  required models. They are absent from the two-mode headset interface.
- With guards on (`?demo=0`, or the desktop page), the right controller must be
  connected with a fresh grip pose each frame, and the newest XR sample must be
  under 150 ms old (`performance.now()` observation time, never the predicted
  display time). Losing tracking, a missing XR frame, a hidden or blurred session,
  or replacing the right controller stops driving; the joystick must be centered
  and held mode buttons released, so a button held through tracking loss never
  fires on recovery. Control is renewed only after `renderer.render()` returns
  (`afterRender`), and new camera freshness counts only once a visible render
  showed it.
- The headset shows only **MANUAL / AUTONOMOUS** and a small **A** toggle hint.
  Full source labels, stop causes, recovery instructions and telemetry stay on
  the laptop page. No actions menu or secondary button mode exists in VR.
- Desktop: select Live rover, hold Space, then use WASD. The on-screen hold button
  can replace Space. STOP, Escape, opening settings, losing focus, or a connection
  failure stop driving.
- Optional navigation paths, goals and frontier markers remain on the desktop
  map. Double-click its floor to set a goal; **Cancel nav** or `C` cancels.
  Headset autonomous mode uses Explore directly; grip-based goals and reticles
  are absent from the minimal interface.
- In settings, select the camera mounting. Servo mode shows the full frame and
  sends head yaw as physical pan; the server and Uno block forward driving while
  pan is more than 10° off center. Fixed-camera mode keeps physical pan at zero
  and shows the entire measured image with its original aspect ratio. The screen
  stays anchored when you turn your head.
  The laptop panel names the mount and video format.
- Mount, H.264, in-VR voice and diagnostics are saved in `localStorage` and can be
  deep-linked: `?mount=fixed|servo&h264=0|1&voice=0|1&diag=0|1`. A query value
  beats the saved choice and is saved; a saved choice beats the default. With no
  mount chosen, `/health.camera_pan: false` selects fixed (`src/viewer-settings.js`).
  With no H.264 choice, `/health.video.enabled: true` selects H.264 for that page
  (unsaved); if no verified decoded frame arrives within 6 s it reverts to JPEG.

## Walk-through map (VR)

`src/map-walk.js` exports the shared `mapWalk`. In walk mode, the reconstructed
map renders in XR at 1:1 scale: the map's low-percentile floor sits on the
headset floor, and the viewer starts at the map origin (the rover start pose).
Head tracking stays free. Walk mode keeps map transfers running while the
deadman is held or the rover explores. Overview mode keeps the earlier
tabletop map, which is hidden in live camera view.

The controls layer drives it:

- `mapWalk.setMode("walk" | "overview")`: AUTO uses walk mode.
- `mapWalk.moveViewer(forward, right = 0, up = 0, turn = 0)`: metres relative
  to the gaze heading, and radians where positive turns left about the head.
- `mapWalk.reset()`: return to the rover start pose.

`?walk=1` starts in walk mode for testing. AUTO drives it from the right stick
(`RoverExperience.roamStep`, 1.5 m/s, 30° snap turns); entering VR resets to the
start pose, and a new map generation resets the viewer. Display only: this module sends no commands and gates no motion.

### Splat walkthrough (`/splat.html`)

A separate page for the post-run RIIS walkthrough. It has no rover sockets or controls, and the driving page is
unchanged. `splat.html?scene=<url>` loads a Gaussian splat (`.ply`, `.splat`, `.ksplat` or `.spz`) with
Spark (`@sparkjsdev/spark`, pinned) inside three.js. On a Quest, press Enter VR (`local-floor`).

- **Controls in VR:** the left stick walks where you look (1.5 m/s, horizontal only). The right stick
  snap-turns 30° about your head. A or X resets to the start pose.
- **Controls on desktop:** drag to orbit and scroll to zoom. Reset view, Flip upright and Open file
  (a local file, which also works in the Quest browser) are in the top bar.
- **URL options:**
  - `type=` sets the format for extensionless URLs.
  - `flip=1` turns a Y-down (COLMAP/3DGS) scene upright.
  - `pos=x,y,z` and `yaw=deg` set the start pose: your floor point and heading in the scene.
  - `sh=0..3` caps spherical harmonics, to trade quality for speed.
- **Serving:** the relay serves anything under `quest/dist`, so a scene copied to `dist/splats/run.ply`
  opens as `/splat.html?scene=/splats/run.ply`. Other origins need CORS.
- **Diagnostics:** `globalThis.__splatStats` reports `{fps, splats, loadMs, xr, error}`, so you can read
  it over CDP on the headset.
- **Verified so far:** desktop Chromium on the Mac loaded the public samples (butterfly `.spz` 177k,
  bonsai `.ksplat` 176k, train `.splat` 1.03M) and a synthetic 300k `.ply` without errors. Quest frame
  rate is not yet measured. Spark applies its WebXR defaults, and fixed foveation is on.

### Mac render in AUTO (`?macview=1`, flat)

Opt-in. With `?macview=1`, AUTO in VR plays the Mac-rendered map
(`tools/mac_render`, PR #132) in place of the local walk-through map. It runs on
the same page, under the same `RoverExperience` and its single `/ws/control`.
Manual right-stick driving and the stick's virtual locomotion are unchanged.
`src/mac-auto.ts` opens only `/ws/macview` (via `H264Viewer`) and
`/ws/macview-pose`. Both are served at the page origin through the relay's
`--render-upstream`.

- **Pose.** The headset pose is mapped into AR-map coordinates, the inverse of
  the walk transform: `p_map = Ry(-yaw) * p_world + (x, floorY + lift, z)`,
  `q_map = Ry(-yaw) * q`. This is what lets the Mac camera follow both head
  tracking and right-stick roam. Poses are latest-only (one in flight, at most
  72 Hz). The math is in `src/mac-auto-pose.js` and the message in
  `docs/interfaces.md`.
- **Picture.** The decoded stream is a flat quad 3 m in front of the head pose
  the frame was rendered from, matched by the renderer's pose echo. The picture
  stays world-locked as the head turns. Without an echo it is head-locked. Its
  size matches the renderer's vertical FOV (50°, or the echoed `fov_y_deg`).
  It is labelled `MAC RENDER · FLAT`. It is not stereo.
- **Stereo (`?macview=1&stereo=1`, stretch).** Asks `/ws/macview?layout=sbs-left-right`
  for packed side-by-side pairs. Each half is a quad seen by only its XR eye
  (three.js layers 1 and 2) with UVs cropped to that half, placed where that eye
  was when the pair was requested and sized to cover that eye's exact
  (asymmetric) frustum at 3 m. Eye poses come from the frame's `XRViewerPose`
  views. Labelled `MAC RENDER · STEREO`. A segment without `stereo`/`layout`
  stays flat. A pair with a bad layout, an unknown or foreign-session pose echo,
  or a missing or invalid eye is never shown as mono; it hides the picture at
  once and the local map returns. Without `stereo=1` nothing changes.
- **Fallback.** While Mac frames are fresh (under 1 s old), the local map
  geometry (points, mesh, scan markers) is hidden and its `/ws/map` transfers
  pause. People markers and nav goals stay drawn over the picture. When frames
  stop or freeze, the local map and its transfers return under a
  `MAC VIEW LOST · LOCAL MAP` label. Only a new decoded frame renews freshness.
  Leaving AUTO closes both Mac sockets.
- **Desk proof** (Chrome, sim map, PR #132 renderer bc155e8, no headset): 1280x720
  decoded and shown 0.8–1.6 s after AUTO entry. After the first frame (which can echo a
  previous session's pose and is head-locked), every displayed frame matched a sent
  pose. Pose-to-display was about 0.9–1.1 s p50, of which 0.6–0.7 s is the
  renderer's segment age at send and about 0.2 s is player buffering. It has not
  been run on a headset.

When the server reports `capped: true` for a Jetson mesh, the 3D view pulls
`/map/points.bin` too, alternating with `/map/mesh.bin` under the same 5 s,
one-at-a-time gate. It no longer derives points from the sparse capped mesh.

## Freshness and bandwidth

The client echoes the rover's opaque, expiring permit at 20 Hz. The server remains
responsible for permit expiry, obstacle checks, and the motor lease. The client
requires a new press after faults; a hidden headset or missing controller never
counts as a physical trigger release.

JPEG freshness is accepted only after decoding and drawing the image to the
camera canvas. Its bound includes server capture age, the complete request and
decode time, and the delay before drawing. Duplicate frame IDs cannot renew it.

Optional H.264 pipelines two bounded TCP fragment requests, checked codec support,
bounded buffers, and restarts after congestion. The decoder's
`requestVideoFrameCallback` media timestamp selects the matching fragment capture
metadata. The freshness bound adds the fragment request duration and elapsed local time.
Only an explicitly measured server queue wait can be subtracted from that duration;
missing or invalid wait metadata uses the full duration. Network time stays included. Without a matched decoder timestamp, motion stays disabled. Quest 2
codec playback still requires a physical headset check.

Capture age at least 500 ms stops movement. A display loop that does not draw for
250 ms also stops movement. Repainting a retained frame keeps the display loop
alive but never renews capture freshness. Switching video formats clears freshness.

Map snapshots and surface downloads pause in live camera view. They resume when
opening the map or settings. Detector packets are slower than the camera, so
`src/detection-match.js` draws the nearest packet from the same source within 8 frames, faded by frame distance,
and never after 500 ms or across sources (an exact frame ID wins; PR #66). Boxes are clipped and transformed in digital-crop mode. H.264 boxes require per-sample capture IDs matching the decoder timestamp; older
servers without those IDs still play video but omit uncertain overlays.

Pose and fall overlays (`src/pose-hud.js`) ride the same matched-frame boxes: when a
detection carries `kpts`, confident limbs (conf >= 0.3, both ends inside the crop) are
stroked as one path per frame; `down: true` adds a red frame with a "PERSON DOWN" tag.
The HUD link chip is red ("LINK LOST") when no control status has arrived for 500 ms,
otherwise shows the server `link_ms` and the detector fps. No pose data is invented.

The local scene assistant accepts text in settings. Optional voice starts only
when clicking Ask by voice, records at most 10 seconds, and sends audio to the rover
for offline transcription. Closing settings or hiding the app cancels capture and
stops all microphone tracks. Missing permission or models leaves text available.
Play answer uses same-origin rover audio, or an explicitly labelled local browser
speech voice; no cloud speech recognition is used. The rescue drill counts
observed person tracks as encounters, rather than claiming distinct people. The
mission report and any saved snapshots come from the rover.

## Rendering budget and diagnostics

Camera, HUD and source-banner canvases are uploaded as textures. To keep upload
spikes out of single frames, `paint()` repaints the camera first whenever it is
due, defers the HUD up to 200 ms while the camera is painting, and paints the
headset banner on a later frame (at most 100 ms after the HUD). HUD and banner
canvases skip repaint and upload when their text is unchanged. Freshness and
liveness timing are unchanged. The diagnostics overlay (`src/diagnostics.js`,
`src/diagnostics-overlay.ts`) is off by default: Settings, the I key or
`?diag=1` (saved like the other viewer settings). It is display only and never feeds the control gate.

## First headset rehearsal

Step-by-step setup and failure checks: [docs/quest-first-run.md](../docs/quest-first-run.md).
These are required physical checks; desktop tests do not prove them.

1. Use the MacBook relay and ADB reverse described in the
   [connectivity plan](../docs/demo-connectivity.md). Open `http://localhost:8080`
   in Quest Browser and confirm that immersive VR is available.
2. Confirm a real camera source and physical serial connection in the HUD. A
   simulated source is labelled conspicuously and must not be presented as hardware.
3. With the car lifted or otherwise prevented from travelling, confirm trigger,
   thumbstick direction, stop release, pan direction, and PWM calibration. Start
   with centered pan and verify a fresh ultrasonic range before forward driving.
4. While holding the trigger, disconnect video, control, and Quest USB in separate
   tests. Restore each connection while continuing to hold it. Motion must remain
   stopped until releasing and pressing again.
5. Open the Quest system menu while holding the trigger. Return with it still held;
   the rover must remain stopped. Cover one controller behind your back, switch a
   controller off and on, and hold Y through the loss: the rover stays stopped,
   the camera stays visible, and nothing acts until each control is released.
6. Test camera/map switching and both mounting modes. In fixed-camera mode, the
   physical pan must remain centered and detections must follow the full image.
7. Test H.264 on the actual headset under the actual cellular path. Observe video
   age and control acknowledgment delay; do not increase the freshness limits to
   hide stalls. Retain JPEG as the compatibility fallback.
8. Run a rescue drill with a real person in view, then inspect the mission report.
   Finally rehearse with venue Wi-Fi disabled on both computers.
9. Click Ask by voice and approve microphone access yourself on the headset.
   Record a short question, select Stop and ask, and check the actual Heard text.
   Confirm closing settings mid-recording stops the microphone. Test Play answer
   with local rover audio; if unavailable, text remains the supported fallback.
   Then select Enable in-VR voice, enter VR, hold the left trigger, ask, and
   release. Check the panel goes listening, processing, answer; that driving
   stops and the right trigger needs a release; that the reply plays in the
   headset; and that taking the headset off mid-question stops the microphone
   indicator. Confirm the source banner reads LIVE only on the real rig and
   SIMULATED or REPLAY otherwise.
