# Farsight Capture for the rover iPhone

This is an original native app. Open `FarsightCapture.xcodeproj` in Xcode, choose
the **FarsightCapture** scheme, and run it on the rover's iPhone. No earlier
project's app or implementation is required.

The ordinary iPhone 17 supplies real rear-camera RGB video to `/phone/rgb`.
It does **not** supply measured scene depth or create a LiDAR map. On a
LiDAR-capable Pro model, the app checks ARKit scene-depth support at runtime and
sends synchronized RGB + Float32 depth + confidence + same-frame camera pose
to `/phone`. The Pro can also select “Send RGB only”. The UI and backend clearly
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
   hotspot network; cellular uplink serves the Jetson → operator connection.
2. On the Jetson, identify the iPhone USB network interface and its assigned IP:
   `ip -br addr`, then `ip route`. This requires the Linux iPhone USB driver and
   phone pairing/tethering working; validate it before the demo.
3. Start the server on the Jetson, bound to `0.0.0.0`, **without `--demo` or
   `--camera`**, so the phone owns the camera source. Configure the Uno serial
   port separately only after its firmware and bench checks pass.
4. Enter `ws://JETSON_USB_IP:8443` in the app and press Start capture. The shown
   default `172.20.10.2` is an example, not an assured address. Use the Jetson's
   actual USB-interface IP, not the phone's gateway IP or its cellular address.
5. Verify sent-frame count increases and `http://JETSON_USB_IP:8443/health`
   reports a fresh camera with `PHONE RGB` or `IPHONE LIDAR`. The MacBook's
   operator relay reaches the same Jetson separately through the configured
   tunnel, then serves the Quest over its USB connection.

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
keep local inference traffic manageable. JPEG quality is 0.5. One frame send
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
`map_epoch: 1`, device name, and explicit `supports_scene_depth` boolean.

RGB packets are `[uint32 LE header bytes][UTF-8 JSON][JPEG]`, with v1
`type: "rgb_frame"`, `session_id`, increasing `frame_id`, monotonic `t_capture`,
`t_wall_ms`, and the actual `jpeg_len`. Extra width/height/tracking metadata is
included. Pro packets use the calibrated v1 `frame` header and append JPEG,
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
