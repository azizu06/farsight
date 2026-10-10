# Quest operator app

The default page opens the live rover camera. `?view=map` opens the retained 3D
map. All requests use the page origin, so the headset stays on the MacBook's USB
relay instead of trying to reach the Jetson directly.

Build with `npm ci && npm run build` from `quest/`. Run `npm test` for protocol,
full-frame geometry, action-menu hit testing, and control safety checks. `npm run test:browser` runs the
production `RoverExperience` in headless Chromium (SwiftShader, simulated XR
poses, generated camera pixels, stubbed microphone, no backend) for resize,
tracking loss, A/B/X/Y actions, menu motion blocking, recentering, transformed
map goal coordinates, left-trigger push-to-talk and source-banner cases;
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

- Quest: everything is on the right controller. Hold the right trigger, then use
  the right thumbstick to drive; release the trigger to stop. The left stick is
  ignored, and the left controller may be idle, asleep or missing without stopping
  the rover. Right-stick input inside a 0.25 deadzone counts as centered, so slight
  drift does not cancel Explore, Seek or go-to. Forward speed is capped at 45% of the server's PWM ceiling;
  calibrate that ceiling on the actual car so motor stiction does not prevent motion.
- Tap right A to switch camera/map (on release). Hold right A for 0.6 s to open
  the actions menu instead (left Y also opens it). Right B stops and cancels
  navigation. Left X recenters the anchored view. In the actions menu, point the
  right controller and squeeze its grip to select Explore, Scan or Drill. The
  menu stops driving. In camera view, right grip selects a pointed person for
  Seek. Release and press the trigger after changing views or selecting an action.
  Head turns command pan only with a servo mount; fixed mode shows the full frame.
- Voice controls appear only when the rover's `/health` reports `assistant.whisper`;
  otherwise they stay hidden and the left trigger only confirms a pending proposal.
  With a Whisper model, hold the left trigger to ask the local assistant by voice;
  release to send.
  First select **Enable in-VR voice** in settings before entering VR: it asks for
  the microphone once and releases it immediately. A question stops driving,
  scan and autonomy, and blocks motion while the microphone is requested or open;
  the right trigger then needs a release. Recording stops at 10 seconds, and
  tracking loss, a controller change, a hidden headset or leaving VR stop the
  microphone. Audio goes only to the rover's `/ask` (local whisper); there is no
  browser speech recognition. Listening, processing, the answer and errors show
  on a panel left of the camera. A proposed Seek or scan acts only after a fresh
  left-trigger press (X dismisses), and still needs the held deadman.
- The right controller must be connected with a fresh grip pose each frame, and the
  newest XR sample must be under 150 ms old (`performance.now()` observation
  time, never the predicted display time). Losing tracking, a missing XR frame,
  a hidden or blurred session, or removing/replacing the right controller stops driving.
  The trigger and every button then need an observed release before acting
  again, so a button held through tracking loss never fires on recovery. The
  camera and stop reason stay visible when only controller tracking is lost.
  Left-controller tracking loss cancels voice and latches its action buttons,
  while a tracked right controller can continue steering.
- A large source banner sits above the camera in the headset and at the top of
  the desktop page. It reads LIVE only while fresh `/health` reports
  `replay: false`, `control.simulated: false`, a connected rover, a fresh real
  camera source and a fresh displayed frame; otherwise NOT VERIFIED LIVE with
  the missing proof. SIMULATED and REPLAY stay shown through disconnects until
  the server explicitly reports otherwise. Its camera source, frame and age are
  display truth only, not sensor or display qualification.
- Directly under the source banner (headset and desktop), a stop panel names
  why motion stopped and the one action that recovers it, e.g. "Command lease
  expired (link lag > 250 ms) → Check Wi-Fi/USB link, then release and hold
  right trigger". `src/stop-reason.js` maps every server `control_status.reason`
  code, `ControlSafety` readiness block and client latch; it is display only and
  hidden while ready or driving. A latch keeps its first specific cause, so an
  echoed `deadman_released` does not hide the real one. Add a mapping there
  (and a test) when adding a server stop code or client latch.
- Desktop: select Live rover, hold Space, then use WASD. The on-screen hold button
  can replace Space. STOP, Escape, opening settings, losing focus, or a connection
  failure stop driving.
- Navigation (optional server `nav` status, see
  [interfaces](../docs/interfaces.md#optional-navigation-status-and-go-to-goals)): the
  planned path glows on the map floor (cyan driving, blue planning, amber
  recovering, green arrived, red failed) with a goal beacon and violet frontier
  markers, in the desktop 3D/2D map and the VR map view. The HUD and the desktop
  panel show backend, mode and state, e.g. `Nav2 · Go-to driving · 2.4 m path`.
  In VR, press A for the map view, point the right controller at the floor (a
  white reticle marks the spot) and press the **right grip** to send a go-to goal;
  **right thumbstick click** cancels navigation in either view. On the desktop,
  double-click the map floor for a goal; **Cancel nav** or `C` cancels. Only a
  `202` arms go-to: then the held deadman sends `mode: "goto"` heartbeats, a view
  change or release keeps the goal armed but needs a fresh deadman press, and any
  stick input, STOP or cancel disarms it and posts `/nav/cancel`. Go-to needs
  `/nav/*` routed to the Jetson; the operator relay must proxy it.
- In settings, select the camera mounting. Servo mode shows the full frame and
  sends head yaw as physical pan; the server and Uno block forward driving while
  pan is more than 10° off center. Fixed-camera mode keeps physical pan at zero
  and shows the entire measured image with its original aspect ratio. The screen
  stays anchored when you turn your head; X recenters it.
  The HUD names the mount and video format. A mode change requires a new deadman press.
- Mount, H.264, in-VR voice and diagnostics are saved in `localStorage` and can be
  deep-linked: `?mount=fixed|servo&h264=0|1&voice=0|1&diag=0|1`. A query value
  beats the saved choice and is saved; a saved choice beats the default. With no
  mount chosen, `/health.camera_pan: false` selects fixed (`src/viewer-settings.js`).

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
