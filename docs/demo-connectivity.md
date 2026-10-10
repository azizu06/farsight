# Cellular demo connectivity plan

We will put an iPhone 17 and the Jetson on the rover, use a second phone beside a
MacBook, and plug the Quest into the MacBook. Both phones supply cellular data
over short USB cables. This removes venue Wi-Fi from the live demo and leaves
the rover free to move.

**Status: implementation plan, pending hardware and venue testing.** Perception,
obstacle checks, motor control, and recording stay on the Jetson. Cellular
operation needs internet access; the perception models do not. Remote viewing
and driving require a working cellular or private local connection.

## Connections and equipment

```text
ROVER                                             OPERATOR

Camera -> Jetson                                   Quest Browser
             |                                        |
             | USB                                    | USB / ADB reverse
             v                                        v
          iPhone 17                                MacBook
             |                                        |
           4G / 5G                                    | USB
             |                                        v
             +-------- cellular internet -------- second phone

Jetson -> USB serial -> Arduino Uno -> motors / pan / ultrasonic
```

Tailscale runs on the **Jetson and MacBook**, connecting them through the phones'
cellular links. A VPN installed only on a phone does not establish this path for
tethered computers. The MacBook serves the built Quest app and relays requests
to the Jetson. There is no cable between the rover and operator.

Bring two tethering-enabled data plans, two phone data cables, a Quest data
cable, and enough MacBook ports or a suitable hub. Prefer a second iPhone:
[Apple supports its USB cellular tethering to a Mac](https://support.apple.com/en-ca/guide/iphone/iph45447ca6/ios).
Keep the Jetson's separate power supply and short Uno USB cable. Check power
headroom while the rover phone charges and inference runs.

Linux has an [iPhone USB Ethernet driver](https://github.com/torvalds/linux/blob/master/drivers/net/usb/ipheth.c),
but the installed Jetson kernel, pairing tools, and iPhone 17 must be tested
together. Android USB tethering to the Jetson is a fallback if that pairing
fails; it also needs a hardware test.

## First prove the transport

1. Install and authenticate Tailscale on both computers. Install Android Platform
   Tools on the MacBook. Enable Quest Developer Mode, approve USB debugging,
   and finish phone trust and tethering setup before judging.
2. Connect the rover phone to the Jetson, enable Personal Hotspot, and verify
   USB networking. Check `uname -r`, `modinfo ipheth`, `ip -br address`, and
   `ip route`. Resolve missing drivers or failed pairing before further work.
3. Connect the second phone to the MacBook and verify its USB network service
   carries internet traffic. Disable Wi-Fi on both computers so testing cannot
   silently use the venue connection.
4. Run `tailscale status` on both computers and `tailscale ping <peer-name>`
   between them. Record whether the path is direct or relayed. Measure sustained
   rover upload and round-trip delay at the actual demo location.
5. Fetch the Jetson health endpoint from the MacBook and exchange WebSocket
   messages through Tailscale before enabling video or motors.

Carrier NAT can force a relay. Successful ping is insufficient: the actual
video and control tests must pass. [Tailscale documents connection types](https://tailscale.com/docs/reference/connection-types).
Restrict rover access to the operator computer through Tailscale policy. Keep
the motor-control server off the public internet.

## MacBook relay and Quest USB connection

Build a small operator relay listening on `127.0.0.1:8080`. It serves the built
Quest app and proxies HTTP and WebSockets to the Jetson service on port 8443
through Tailscale. Configure the upstream address and scheme on the MacBook.
Preserve certificate verification when the upstream uses HTTPS.

Every Quest request uses the local page origin: health, video, control,
detections, mapping, assistant requests, and returned audio URLs. Rewrite
absolute upstream URLs when needed. The headset must not bypass USB by
connecting directly to a Jetson hostname or Tailscale IP. Bundle frontend assets
locally and preserve both the mapping and rover endpoints used by the app.

After starting the relay, run on the MacBook:

```sh
adb devices
adb reverse tcp:8080 tcp:8080
adb reverse --list
```

Open `http://localhost:8080` in Quest Browser. Confirm `window.isSecureContext`
and immersive VR support on the actual headset. Localhost is a
[potentially trustworthy browser origin](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Secure_Contexts).
If this headset setup requires HTTPS, configure trusted localhost HTTPS on the
relay. Reapply the ADB mapping after a reconnect or reboot.
[Meta documents the USB workflow](https://developers.meta.com/vr/documentation/web/browser-remote-debugging/).

ADB reverse carries TCP. Forwarding the page port does not forward WebRTC's UDP
media. Our initial video implementation will use a TCP-compatible stream. Any
later WebRTC implementation must terminate on the MacBook and explicitly bridge
media to the headset, or provide another tested TCP-compatible media path.

## Camera and video bandwidth

Use the existing USB webcam for the first transport check, or the phone camera
if its capture app is already working. Camera and inference traffic must remain
on the rover. An iPhone capture app needs an explicit USB application connection
to the Jetson; Personal Hotspot does not automatically turn it into a webcam or
forward its RGB-D stream. Verify that path separately if using phone LiDAR.

Use the existing `/stream.mjpg` route initially at reduced resolution and frame
rate, and measure its bitrate. The older 640x480 at 30 fps MJPEG target is not
our cellular bandwidth budget.

The current implementation sends H.264 fragmented MP4 over `/ws/video`, relayed
by the MacBook. Quest feeds it to MediaSource and displays a video texture.
Initialization and codec metadata precede fragments; recovery starts a new
decodable stream. Check
`MediaSource.isTypeSupported()` against the encoder's actual codec string and
measure playback latency on Quest before adopting this format.
[MediaSource documentation](https://developer.mozilla.org/en-US/docs/Web/API/MediaSource)
describes the playback mechanism; headset compatibility remains a test gate.

These are the current software defaults, subject to real-rig qualification:

| Setting | Target |
|---|---|
| Resolution and frame rate | 640x480 at 10 fps |
| H.264 bitrate | 500 kbps; 200 ms encoder VBV |
| Keyframe interval | At most 1 second |
| Encoder settings | Low latency, no B-frames |
| Sustained rover upload | At least twice the measured total outbound bitrate |

Reduce bitrate, resolution, and frame rate when bandwidth or latency deteriorates.
Bound queues. If encoded video falls behind, reset the video connection and
resume from initialization data plus a keyframe; dropping arbitrary dependent
H.264 frames corrupts playback. Match detections to video timestamps and label
stale data. Pause point-cloud transfers and large audio/data uploads during
driving if they interfere with control.

The Orin Nano **does not have NVENC**. Start with a measured CPU H.264 encoder,
or camera-provided H.264 only if the webcam exposes a suitable mode.
[NVIDIA documents software encoding on Orin Nano](https://docs.nvidia.com/jetson/archives/r36.4.3/DeveloperGuide/SD/Multimedia/SoftwareEncodeInOrinNano.html).
The hardware H.264 claim in the older project plan must not be used as an
implementation assumption.

If Jetson encoding costs too much CPU, use a native iPhone camera app to encode
the live view in hardware and send sampled frames over USB for Jetson inference.
[VideoToolbox](https://developer.apple.com/documentation/videotoolbox)
provides hardware encoding; [libusbmuxd port forwarding](https://github.com/libimobiledevice/libusbmuxd)
is a candidate USB application connection. This requires app and integration
work. Qualify the simple webcam path first unless phone capture is already ready.

## Control freshness and link failure

Video and control use independent connections. They still share cellular
capacity, so reserve bandwidth for controls and never queue old joystick input.
Forward the latest input at the existing 20 Hz rate.

Preserve the [rover contract](interfaces.md): the Jetson stops after 250 ms
without valid control; the Uno stops after 300 ms without motor commands. Do
not lengthen timeouts to conceal cellular stalls. Obstacle checks and serial
control remain local.

Sequence numbers alone do not make delayed commands fresh. Before live driving,
add a session ID and an expiring Jetson-issued control permit echoed by the
client. Check permit expiry using the Jetson's monotonic clock, so buffered
commands cannot renew motion after expiry. Specify its lifetime and renewal
behavior in the contract and test it under delay. A new connection starts
stopped and requires a new deadman press. Never replay motor commands.

Show video age, control delay, and connection state in the HUD. Stop driving
when video becomes stale. The MacBook relay stops renewing control when the
headset reports stale playback or headset liveness expires. Continue recording
on the rover during an outage. Assistant inference stays local, but delivering
the question and spoken answer to Quest still requires the remote connection.

## Implementation order

1. Qualify both USB-tethered phones, Tailscale, and USB WebXR with Wi-Fi disabled.
   Record tested OS versions, cables, carriers, routing, and venue measurements.
2. Add the MacBook relay and same-origin Quest configuration. Check every
   endpoint with the rover stationary, including the phone capture path if used.
3. Measure reduced MJPEG, implement and qualify H.264 playback, and establish
   a bandwidth budget with inference running.
4. Add expiring control permits, stale-video handling, and stopped reconnection.
   Specify additions to `interfaces.md` when implementing them. The new video
   route and permit fields above are proposals, not existing endpoints.
5. Rehearse the complete demo at the venue under load and deliberate failures.

## Demo qualification and fallback

Run a 15-minute battery rehearsal with inference, streaming, Quest head tracking,
and controls active. Record median and p95 round-trip delay, displayed video age,
actual bitrate, inference rate, CPU load, and stop timing. Use visible timestamps
or a physical timer for camera-to-display delay; ping does not measure that.

Initial acceptance targets are p95 displayed video age below 400 ms and no
uncommanded movement. Treat video older than 500 ms as stale and stop driving.
The 250 ms control lease must work without repeated nuisance stops in normal
operation. If it cannot, this connection is unsuitable for continuous teleop.
Measure the Jetson lease stop and Uno watchdog stop independently.

Disconnect each phone, Quest USB, and video in turn. Add bandwidth limits and
bursty delays. Verify that motion stops, delayed commands are rejected, stale
video is labelled, and reconnection never resumes motion. Cold-boot once with
venue Wi-Fi unavailable.

Try a different underlying cellular carrier if the first is congested. Prepare
a private 5 GHz hotspot as another path: connect Jetson and MacBook locally and
test peer reachability, since hotspot client isolation can block it. Leave
iPhone Maximize Compatibility off and verify the actual band:
[Apple notes that enabling it disables 5 GHz](https://support.apple.com/en-ca/guide/security/secfd166f620/web).
Keep Quest USB connected in either mode and change only the relay upstream.
Stop and require a new deadman press after switching paths.

If every wireless path fails, show a clearly labelled recorded run and retained
mission data with motion disabled. This preserves an honest presentation, not
live teleoperation. An untethered rover cannot send live video through a complete
radio outage.
