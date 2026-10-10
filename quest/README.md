# Quest app

Issues #4, #8, #10 and the software core of #16. All source was written during Knight Hacks IX.

## Run on a laptop before the headset arrives

Use Node 22 or newer. Start the team's mock server (#2) or Jetson server first, then:

```sh
cd quest
npm ci
FARSIGHT_JETSON_ORIGIN=https://YOUR-JETSON:8443 npm run dev
```

Open `http://localhost:5174/?view=vr` for the rover/VR screen. The existing mapping viewer remains at `/` and links here from its settings. The app proxies the camera, control/detection WebSockets and existing mapping/phone endpoints through this origin; the headset never contacts a different host. An explicitly known self-signed upstream can be allowed with `FARSIGHT_ALLOW_SELF_SIGNED=1`. This affects the relay's upstream connection only, and does not make the browser-facing certificate trusted.

For a mock, open `/?view=vr&source=mock`; recorded runs use `/?view=vr&source=replay`. These show visible badges in both the browser and headset. They do not generate images, detections or a synthetic substitute when a live feed fails.

```sh
npm run build       # outputs quest/dist for the Jetson/relay to serve
npm run preview     # same proxy options as dev
```

## Quest connection

The current team connection plan is [docs/demo-connectivity.md](../docs/demo-connectivity.md): MacBook relay, same-origin requests, and Quest USB/ADB reverse. The app's dev/preview server can be used as that relay while iterating. Substitute port 5174 for the example page port:

```sh
adb reverse tcp:5174 tcp:5174
```

Open `http://localhost:5174/?view=vr` in Quest Browser and check `window.isSecureContext` and VR availability on the physical headset. For a trusted HTTPS origin instead, supply both certificate paths:

```sh
FARSIGHT_TLS_CERT=/absolute/path/cert.pem \
FARSIGHT_TLS_KEY=/absolute/path/key.pem \
FARSIGHT_JETSON_ORIGIN=https://YOUR-JETSON:8443 \
npm run dev
```

Certificate trust, ADB reverse and immersive entry must be verified on the actual Quest. Desktop software checks do not establish those.

## Controls and display

- Driving starts disabled. Enable it deliberately, release the deadman, then hold it to move.
- Quest: right trigger is deadman; right thumbstick drives and turns. Head yaw requests camera pan, relative to initial facing, smoothed and clamped to ±80°. Left trigger points/selects the drill and stop menu. Physical pan direction and controller behaviour still need hardware checks.
- Desktop: click the camera preview, hold Space, and use WASD. Releasing Space sends a neutral command immediately.
- Lost control replies after 500 ms, lost XR tracking/input, blur, hidden page or outbound backlog disarms local intent. Reconnect and release controls before trying again.
- Video is a mono curved screen, with the same image in both eyes. It does not provide stereo depth or a 360° view.
- No new displayed camera image for two seconds clears it and disables driving. This measures local frame arrival, not camera exposure time or network queue age; the present MJPEG contract has no source capture timestamp.
- Detection boxes use normalized image coordinates. Video and detection messages arrive independently; exact same-frame overlays cannot be guaranteed by this contract.
- Detector fps and link milliseconds are received telemetry, not locally measured glass-to-glass latency.
- The Jetson's 250 ms lease and Uno's 300 ms watchdog remain essential. Closing a browser WebSocket cannot retract bytes already queued by the network. Test real stopping separately; do not treat browser tests as motor-stop measurements.

## Rescue drill status

The 90 s timer, one-second continuous face dwell, stable-ID deduplication and safety-stop counter are implemented. Face scoring defaults disabled until the detector owner approves and publishes `cls: "face"` with stable IDs. After that contract approval, build with `VITE_FACE_CLASS_APPROVED=1` and verify against #9. Enter the actual number of placed victims; the app does not invent a total or count people as faces.

Command breadcrumbs are bounded history in normalized command-seconds, explicitly approximate and unmeasured. They are not a physical map or a calibrated route home. Issue #16's useful arrow trail still needs the rover's measured speed/turn calibration or another position source. Safety-stop counts do not prove how many collisions were avoided.

## Checks without hardware

```sh
npm test
npm run build
npx playwright install chromium
npm run test:browser
```

The browser suite runs a labelled external MJPEG/WebSocket fixture on 18900 and an isolated Vite relay on 5214. It checks actual rendered JPEG pixels, telemetry, keyboard release, lost link, stale image clearing, graphics loss, delayed decoding recovery, changing image resolution and a late XR grant after viewer disposal. It also checks that the default mapping screen still links to the rover view. It never connects to a real rover. The suite requires Chromium's normal system libraries.

Before closing the assigned issues, check on real hardware: trusted-origin/ADB entry, legible placement, left-menu/right-drive interaction, head pan sign/range, visual latency, deadman release, Wi-Fi loss, graphics/tracking loss, and the Jetson/Uno stop lease. Record measurements rather than assuming target fps or stop times.
