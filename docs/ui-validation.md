# Desktop UI validation — October 10, 2026

Room/control checks were observed between approximately **6:07 and 6:28 AM Eastern**
(10:07–10:28 UTC). A positive model fixture was added at approximately
**6:32–6:42 AM Eastern**.
The operator origin was `http://localhost:8080`, with its rover upstream on
`127.0.0.1:18444`. The tested page loaded `/assets/index-uCvGUTDk.js`, built from
the `a520463` frontend. Repository HEAD advanced to `662ca9c` during the checks;
a later merge/build needs its own regression check. The later isolated photo fixture
used a fresh successful production build, `/assets/index-Brk5Om09.js`.

Chrome supplied the available browser surface. IAB was unavailable. A separate
temporary validation tab preserved the user's existing tabs. No microphone
permission or personal audio capture was requested. The source and control
backend explicitly reported `SIMULATED`.

## Observed runtime evidence

| Check | Observed result | Evidence scope |
|---|---|---|
| Latest JPEG display | A visible moving room view; HUD Ready; sampled UI ages 103 and 198 ms | Desktop canvas and reported receipt-age bound |
| JPEG deadman/input | Space+W produced 26 active status samples at left/right PWM 35/35; final output 0/0 with `deadman_released`; 174 samples total | Actual browser → relay → virtual control state; no physical motor claim |
| H.264 decode after encoder recovery | Visible moving room view; Ready; observed UI ages 215, 230, 354 and 428 ms; camera canvas 640×480 | Actual MediaSource/browser decode and draw, using the two-request path |
| H.264 deadman/input | Space+W produced 36 active status samples at 35/35; final output 0/0 with `deadman_released`; 175 samples total | Actual browser → relay → virtual control state |
| Tab/controller disconnect | The first temporary tab disappeared; a subsequent 170-sample trace remained 0/0 with `controller_disconnected` and `requires_release: true` | A real software socket-close case; exact physical stop timing was not measured |
| Detector/video identity match | H.264 HUD displayed `0 detections`, which the current code emits only for a matching cached source/frame packet | Empty-result alignment was observed; a positive drawn box was not observed |
| Positive detections | A separate live `/ws/detections` observer received 270 packets over 90.03 seconds with no nonempty boxes | HOG/procedural-room observation only; positive-overlay proof remains pending |
| Runtime health | `video.enabled=true`, `ready=true`, `error=null`, 10 fps, 500000-bit bitrate; camera fresh; HOG about 3–3.2 fps | Current local server health, rather than an old encoder's retained init |
| Browser console | No application warning/error entries returned by the browser log reader | These inspected local runs |

UI video age is the software receipt/encoding/transit bound described in
[interfaces.md](interfaces.md). It does not prove sensor-exposure-to-headset
latency. The listed values are individual observations, not a measured p95.
The motor traces show nonzero virtual output followed by zero output; they do
not establish a physical 250/300 ms stop-time result.

The decoded H.264 screenshot was saved to
`/tmp/farsight-ui-validation-h264.jpg` as a local, temporary evidence artifact.
It is a simulated camera view. The screenshot is outside the repository.

## Canonical Quest PR review

[PR #32](https://github.com/azizu06/farsight/pull/32), reviewed at
`81764f7262a31dbac4ff5f04b031da587675ce6b`, preserves current-animation-frame
`getViewerPose` use and adds:

- Connected gamepad and current grip-pose checks for the right deadman source
  and the selected movement source, source replacement invalidation, and a
  150 ms XR input-sample deadline. Missing tracking does not count as a release.
- Recreated `CanvasTexture` plus a reassigned/updated material map when canvas
  dimensions change. This addresses JPEG 320×240 → H.264 640×480 after an XR
  texture upload; the currently tested desktop canvas does not prove that GPU path.
- A committed browser regression that checks actual WebGL pixels after a
  640×480 → 1280×720 resize, along with synthetic XR tracking/controller-loss
  cases. The PR author's reported passing run was not independently executed
  during this review.

The pending canonical mapping is **A: person Seek/contextual camera-map toggle,
B: Explore, left X: center pan**. The tested asset and current rehearsal wording
still use A camera/map and B center. Align the served build, HUD hints, and
rehearsal checklist after the canonical PR is integrated.

## Remaining rig gates

Quest 2 presentation, controller mapping and grip tracking, first-frame pan
centering, texture resize in XR, positive box/pose overlays, microphone capture,
physical camera-to-display delay, both USB-tethered phones/Tailscale, and actual
Uno/motor/watchdog stop timing require the real rig. Rehearse held-trigger
recovery, system-menu visibility changes, controller loss, and video/control
failure with the final integrated build. A real camera run remains necessary for rig-level matched-box evidence. The
later photo fixture below establishes positive JPEG software rendering; positive
H.264 raster overlays remain inconclusive.


## Later genuine-model positive fixture

A separate production `create_app` instance ran on `127.0.0.1:18546`, with an
operator relay on `localhost:18086`. It used repository `models/yolo11n.onnx`
through the actual `PerceptionService`/OpenCV ONNX code. The input was the
Ultralytics package's bundled `assets/bus.jpg` from the installed Python 3.11
package. A temporary producer published that unmodified photo at 15 fps with
increasing frame IDs and an explicit `SIMULATED` camera source. The virtual
rover opened no physical serial port. No bounding boxes or predictions were
manually inserted.

The actual model reported four person tracks at approximately 90%, 85%, 84%,
and 40% confidence plus a bus at 94%. Detector health reported ONNX at about
3 fps, fresh camera input, and an active 10 fps / 500000-bit H.264 encoder.

- **Positive JPEG rendering verified:** the captured image visibly contains
  green rectangles and actual class/track/confidence labels aligned to the four
  people and bus. `/tmp/farsight-ui-jpeg-boxes-0.jpg` was inspected directly.
  The small bright-green pixel count from the compressed screenshot was also
  observed; the visible rectangles and labels provide the substantive evidence.
- **Positive H.264 rendering remains inconclusive:** the photo decoded and
  the HUD intermittently reported a matching five-detection packet. Eleven
  screenshots of those caption phases showed no visible green raster boxes.
  A matching caption alone is insufficient to certify visible overlays.
- **Digital crop partially checked:** fixed-camera mode selected the measured
  70% crop, with its explicit digital-pan label. A positive cropped-box phase
  was not captured before shutdown. `/tmp/farsight-ui-crop-view.jpg` records
  the view; crop geometry unit tests supply separate software evidence.

Both isolated fixture processes exited with code 0, and the temporary fixture
browser tab was closed before the final quiet transport rehearsal. The existing
operator server and the user's browser tabs were preserved. The fixture is a
public test photograph and a simulated producer, rather than a live rover or
physical rescue observation. All screenshot artifacts are temporary local files
outside the repository.
