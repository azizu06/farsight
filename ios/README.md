# Farsight Capture for the rover iPhone

The app also implements the confirmed [phone-owned cellular relay](../docs/phone-relay.md).
Choose **Rover** to run capture and TCP forwarding together, **Operator** for the
USB-accessible relay without camera permission, or **Off** for legacy capture.
Both phone VPN/listener/USB hops still require actual device qualification.
The bridge defaults off and leaves HTTP/WebSocket messages unchanged.
Operator mode supports the rover-phone primary plus an optional Jetson tailnet
fallback, disabled by default. Each TCP session keeps its selected route;
failures close it without replay. The UI shows primary/fallback session counts.
An explicitly started operator listener re-arms after foregrounding; manual
Stop or a role change clears that intent. Capture never auto-resumes. The optional
**Pinned dual routes for host broker** mode binds primary loopback8081 and
fallback loopback8082 separately for the experimental bonded Mac relay; it is
mutually exclusive with automatic fallback and remains off by default. Both
listeners must bind before accepting pairs, and a bind failure closes both.
In Rover mode, capture and forwarding use the same validated Jetson USB address;
changing role or addresses requires stopping active capture/forwarding first.

This is an original native app. Open `FarsightCapture.xcodeproj` in Xcode, choose
the **FarsightCapture** scheme, and run it on the rover's iPhone. No earlier
project's app or implementation is required.

The ordinary iPhone 17 supplies real rear-camera RGB video to `/phone/rgb`
through AVFoundation. RGB-only mode is also the default on a Pro.
It does **not** supply measured scene depth or create a LiDAR map. On a
LiDAR-capable Pro model, deselecting “Send RGB only” checks ARKit scene-depth
support at runtime and sends synchronized RGB + Float32 depth + confidence +
same-frame camera pose to `/phone`. That mode is view-only for physical
driving unless the server runs with `FARSIGHT_LIDAR_DRIVE=1`. The UI and backend clearly
identify the current source. Runtime support, rather than the typed phone model,
selects the mode.

## Install on the phone

1. Open this project's `.xcodeproj` in Xcode.
2. In Signing & Capabilities, select your Apple development team. If necessary,
   change `dev.farsight.capture` to a unique bundle identifier for your team.
3. Connect the phone to the MacBook, trust it, enable Developer Mode if Xcode
   requests it, select that phone as the run destination, and click Run.
4. Allow Camera and Local Network access. The app only uses the rear camera;
   no microphone permission is needed.
5. Stop capture, disconnect it from the MacBook, then cable the phone to the
   Jetson's USB port. The app remains installed and launches from its icon.

Physical-device installation requires signing. The unsigned build below checks
that the complete iOS target compiles; it is not an installable signed app:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild \
  -project ios/FarsightCapture.xcodeproj -scheme FarsightCapture \
  -configuration Debug -destination 'generic/platform=iOS' \
  -derivedDataPath /tmp/farsight-ios-build CODE_SIGNING_ALLOWED=NO build
```

The project targets iOS 17+, uses only Apple frameworks, and has no package
downloads. ARKit capture requires a physical phone; the simulator cannot prove
camera, LiDAR, or USB networking behavior.

## Connect locally to the Jetson over USB

1. On the rover phone, enable cellular data and Personal Hotspot / Allow Others
   to Join. Trust the Jetson when asked. This local app connection uses the USB
   hotspot network. In the primary design, the rover phone owns the cellular
   relay to the operator phone; raw capture stays on USB.
2. On the Jetson, identify the iPhone USB network interface and its assigned IP:
   `ip -br addr`, then `ip route`. This requires the Linux iPhone USB driver and
   phone pairing/tethering working; validate it before the demo.
   `jetson/setup.sh` now prepares available `usbmuxd`, `libimobiledevice-utils`
   and `ipheth-utils` packages. On a minimal image, verify them before relying
   on the phone for downloads. Unlock the phone and approve Trust. Use
   `idevice_id -l` to identify the rover phone, then
   `idevicepair -u ROVER_PHONE_UDID validate`; if pairing is absent, deliberately
   run `idevicepair -u ROVER_PHONE_UDID pair` and approve the phone's prompt.
   These helpers do not prove the IP route or available cellular throughput.
   If they fail, retain the exact error; do not automatically unpair/reset the
   phone. [Ubuntu's tether helper](https://packages.ubuntu.com/noble/net/ipheth-utils)
   and the [upstream pairing tool](https://github.com/libimobiledevice/libimobiledevice/blob/master/docs/idevicepair.1)
   describe the package and pairing operations.
3. Start the server on the Jetson, bound to `0.0.0.0`, **without `--demo` or
   `--camera`**, so the phone owns the camera source. Configure the Uno serial
   port separately only after its firmware and bench checks pass.
4. Enter `ws://JETSON_USB_IP:8443` in the app and press Start capture. The shown
   default `172.20.10.3` is the last observed address, not an assured address. Use the Jetson's
   actual USB-interface IP, not the phone's gateway IP or its cellular address.
5. Verify sent-frame count increases and `http://JETSON_USB_IP:8443/health`
   reports a fresh camera with `PHONE RGB` or `IPHONE LIDAR`. The MacBook's
   operator relay reaches the same Jetson separately through the configured
   tunnel, then serves the Quest over its USB connection.

Exactly one camera owner runs at a time: stopping/mode change pauses ARKit and
stops AVCapture before the selected path starts. Mode changes require Stop.
The app uses landscape-right native image coordinates. Mount the phone in that
orientation and check the live preview is upright before attaching it to the
rover. Pixels are resized proportionally, never cropped or independently
rotated relative to depth. Pro intrinsics scale to the transmitted JPEG;
matrices are column-major ARKit camera-to-world, Y up and camera view down -Z.
Depth/confidence buffers are copied row by row to remove pixel-buffer padding.

Use a trusted `wss://` endpoint if desired; certificate checks remain enabled.
Plain `ws://` is enabled for the local USB network. This app does not bypass
certificate errors or require venue Wi-Fi.

## Bounded live transport

RGB targets 15 fps at maximum 640 pixels wide; measured RGB-D targets 6 fps to
keep local inference traffic manageable. Start cellular driving in RGB-only
mode, including on a Pro. Qualify the lower-rate RGB-D path separately before
driving with it: older frames leave less margin inside the 500 ms video guard.
Stop capture before changing modes. JPEG quality is 0.5. One frame send
can be in flight. New frames are skipped during that send; they are never kept
for replay. A send that remains incomplete for 500 ms closes the socket. Any
disconnect, camera interruption, or backgrounding stops capture; press Start
to create a new phone session and ARKit world origin. The app does not reconnect
automatically and replay old frames. Stop capture before opening another phone
camera connection.

The backend's latest-frame camera timestamp and motor guard are independent
of ARKit pose heartbeats. Stopped frames cannot keep motion enabled merely by
maintaining tracking. Test phone unplugging and backgrounding with the wheels
lifted before any floor driving.

## Wire format

Every connection starts with a text JSON v1 `hello`, a new UUID `session_id`,
`map_epoch: 1`, device name, explicit `supports_scene_depth`, and
`supports_capture_permits: true`. Both packet modes include `map_epoch: 1`.

RGB packets are `[uint32 LE header bytes][UTF-8 JSON][JPEG]`, with v1
`type: "rgb_frame"`, `session_id`, increasing `frame_id`, monotonic `t_capture`,
`t_wall_ms`, and the actual `jpeg_len`. Extra width/height/tracking metadata is
included. Both modes optionally include `producer_delay_ms`: a local monotonic
interval from entry into the shared frame-processing helper through
JPEG/depth/confidence buffer construction, sampled before final JSON framing.
It includes local gate/encoding work and excludes camera work before the helper,
sample-clock conversion, final framing, send queue/network time and server
decode/queue time. Capture-permit provenance is a separate bound.

## Capture permits and sensor timestamps

The RGB path uses Apple's documented clock relationship, rather than guessing
ARKit's timestamp origin. All AVCapture output sample timestamps use the
[`synchronizationClock`](https://developer.apple.com/documentation/avfoundation/avcapturesession/synchronizationclock).
[`CMSyncConvertTime`](https://developer.apple.com/documentation/coremedia/cmsyncconverttime(_:from:to:))
converts the actual sample presentation timestamp from that clock to
`CMClockGetHostTimeClock()`. Permit receipt and callback time are sampled with
`CMClockGetTime` on that same host clock. Neither time is fabricated or shifted
by an estimated offset.

A permit-capable server sends JSON like:

```json
{"type":"capture_permit","session_id":"current UUID","map_epoch":1,
 "token":"opaque server token","valid_for_ms":250}
```

Wrong-session, wrong-epoch or malformed permits are ignored. Duplicate tokens
never renew their receipt time. Up to eight live tokens are retained; the gate
selects the newest token received before the sample exposure. A newer token
that arrived after that exposure does not hide a valid predecessor. After negotiation, RGB transmission requires
12 consecutive finite, advancing sensor/host samples with sensor age in
0..150 ms, actual converted exposure strictly **after** permit receipt, and
local token age less than its advertised validity (at most 250 ms). The gate
checks again after encoding/framing; old callbacks and expired encoding work
are skipped. AVCapture also discards late video callbacks. Stop, interruption
and a new session clear token and clock evidence.

Bound RGB packets echo `capture_permit` and `capture_permit_received_t`, set
`capture_clock: "avcapture_sync_to_host"`, `capture_clock_verified: true` and
`capture_bound: true`. `t_capture` is the actual converted sensor timestamp.
Diagnostics include `sensor_timestamp_raw_t`, `sensor_timestamp_host_t`,
`callback_host_t`, `sensor_host_delta_ms` and `sensor_age_ms`. The phone shows
raw sensor/host samples and their delta for actual-device verification. The
server must independently recognize the token and its issue/expiry; the phone's
local check does not renew or extend the server deadline.

Before any server permit arrives, legacy backends can still receive camera
images, with `capture_bound: false` and a conspicuous **Legacy view only** status.
That compatibility path is not sufficient for physical drive. The coordinated
backend must require qualified capture provenance before enabling physical
motors. A permit-capable RGB session never falls back to unbound sending after
its first valid permit.

Apple's [ARFrame timestamp contract](https://developer.apple.com/documentation/arkit/arframe/timestamp)
does not promise this same host-clock origin. ARKit LiDAR packets therefore keep
the original timestamp, report `capture_clock: "arkit_unverified"`,
`capture_clock_verified: false`, `capture_bound: false`, and show an explicit
view-only diagnostic. A numeric AR/host delta is displayed for investigation;
it is not treated as a clock conversion or a driving qualification. The backend
views and fuses those frames; physical motion on them needs the server's opt-in
`--lidar-drive` (`FARSIGHT_LIDAR_DRIVE=1`), which accepts fresh frames with
normal tracking without a verified capture clock.

The unsigned build and deterministic Swift tests prove compilation, permit
parsing, exposure ordering and bounds; they do not prove phone sensor timing,
camera orientation, USB delivery or real-device capture performance. Measure
actual headers on the installed phone before declaring that path qualified.

Pro packets use the calibrated v1 `frame` header and append JPEG,
little-endian Float32 measured depth, and UInt8 confidence as specified in
`docs/mapping.md`. No estimated or simulated depth is sent.

Protocol verification:

```sh
python3 -m unittest discover -s ios/tests -v
```

These checks compile the production Swift packet builder and send its output
through the actual Python decoder/app. Real sensor alignment, physical device
installation, phone-to-Jetson USB routing, and live floor operation still need
the real devices.
