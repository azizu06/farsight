# Phone-owned cellular relay

The confirmed design puts the cellular connection on the two iPhones. The Mac
and Jetson use local USB connections. The primary native two-phone path has
delivered live camera, detection and control traffic with Wi-Fi off and the
Mac-to-Jetson commissioning USB unplugged. Physical driving, Quest playback and
fault recovery still need the complete rig rehearsal. The optional direct
Jetson tailnet path is a fallback; it is not required for the phone-owned path.

```text
Quest Browser
  ↕ USB ADB reverse
Mac operator relay :8080
  ↕ localhost :18443 → USB-only iproxy
Operator iPhone localhost :8081
  ↕ phone Tailscale over cellular
Rover iPhone Tailscale address :8081
  ↕ fixed TCP forwarding over local USB network
Jetson :8443 → inference, mapping, control → Uno USB serial
```

The rover phone also captures RGB/ARKit/LiDAR in the same foreground app and
sends it directly to the Jetson's local USB address. Raw capture does not cross
cellular. Only the existing processed video, maps, status and control traffic
crosses between phones. HTTP/WebSocket protocols and stop leases stay unchanged.

The operator phone's listener is device loopback, reached through usbmux rather
than hotspot DHCP. The rover listener is bound to its phone Tailscale address,
with a fixed Jetson USB upstream. Neither phone is an arbitrary destination
proxy. The computers do not need Tailscale for this path.

## Bring-up order

1. Install and trust the Farsight Capture development app on both phones. Enable
   Developer Mode as required by iOS. The operator phone needs no camera capture.
2. Join both phones to the same authorized tailnet and enable their Tailscale
   VPNs. Record the **phones'** Tailscale IPv4 addresses; the Jetson's existing
   `100.103.169.40` address belongs to the fallback and is not the rover phone.
3. On the rover phone, select the rover bridge role, enter its own Tailscale
   address and the actual Jetson USB address, then start the bridge. Start
   capture to that same local Jetson address. Its local USB networking currently
   uses Apple's Personal Hotspot setting; this does not send raw capture over
   cellular. The last observed Jetson USB address was `172.20.10.3`, not `.2`.
4. On the operator phone, select the operator bridge role, enter the rover
   phone's Tailscale address and start the bridge. Keep both apps foreground.
5. Pair/trust the operator iPhone with its Mac. Install host tools once:

   ```sh
   brew install libusbmuxd libimobiledevice
   ```

   `iproxy` forwards the Mac's loopback TCP port through the USB cable to the
   phone's loopback listener. No operator hotspot IP or default route is needed.
6. Run a bounded USB health check first; it stops its proxy when finished:

   ```sh
   python -m tools.phone_usb --check
   ```

   Then keep the helper running in one terminal:

   ```sh
   python -m tools.phone_usb
   ```

   Start the existing operator relay in another terminal:

   ```sh
   python -m tools.demo --upstream http://127.0.0.1:18443 --quest
   ```

   Quest still opens `http://localhost:8080` through ADB reverse. When multiple
   iOS devices are attached, add `--device DEVICE_ID` to the USB helper; obtain
   the identifier locally and do not post it publicly. The helper always uses
   USB-only enumeration, an explicit device selector and loopback binding.
   It refuses old tools lacking those controls and remote usbmux daemons.
   Its health probe does not open a control socket or request motion; success
   proves a forwarded backend response, not the carrier path or all bridge hops.

## Required evidence

Software integration evidence is saved in
[phone-bridge-chain.json](qualification/phone-bridge-chain.json). Two unchanged
production Network.framework bridges ran in series on macOS loopback, between
the production operator relay and a simulated Jetson server. HTTP/JPEG,
detections and six ordered H.264 fragments passed; simulated release, lease
expiry, disconnect and stopping either bridge returned zero output. Restart
kept zero output and required release. Run the reproducible test with
`python -m unittest tools.tests.test_phone_bridge_chain -v` on a Mac with
Swift/Network.framework and the documented Python/FFmpeg dependencies.
It uses no phone, usbmux, cellular, Quest or physical motor.

Compilation and loopback tests do not prove iPhone USB/VPN interoperability.
Before switching the demo to this path, verify all of these on the actual rig:

- Mac reaches the operator phone listener through USB with hotspot DHCP off.
- Operator phone reaches the rover phone listener through its native VPN.
- Rover phone reaches Jetson locally while capture and forwarding coexist.
- Native phone ping reports direct versus DERP; moving endpoints to phones does
  not guarantee a direct cellular path or lower latency.
- Live video, detections and maps reach Quest with computer Wi-Fi unavailable
  and the Mac-to-Jetson commissioning USB unplugged.
- Disconnect either phone/cable, background either relay app and interrupt
  cellular. Existing 250 ms control leases and 500 ms displayed-frame freshness
  must stop motion, with explicit release before resuming.
- Run the complete load rehearsal and physical camera-to-display timing test.

The existing `tools.qualify_route` checks **USB tethering/default routes** and
belongs to the fallback architecture. It does not qualify this phone-owned
path, whose Mac upstream is localhost. Physical camera eligibility remains
separate: current ARKit capture has not yet qualified its exposure clock for
the new physical-driving gate. Keep that task visible alongside transport work.

## Direct operator path (opt-in)

The operator phone can skip the rover phone and talk straight to the Jetson's
Tailscale address. Reason: the Jetson-to-operator-phone leg can be a direct
path (measured IPv6, ~90 ms) while the rover phone is behind DERP, so removing
the middle hop removes one relay hop and one app that iOS can background.

```text
Operator iPhone localhost :8081 ──phone Tailscale──▶ Jetson 100.x.y.z:8081
Jetson farsight-tailnet-forward ──▶ 127.0.0.1:8443 (unchanged server)
```

Install once on the Jetson (opt-in; never touches `farsight.service`, the
firewall or Tailscale settings; sudo asks for your password unless it is passwordless):

```sh
bash jetson/install_tailnet_forward.sh        # --dry-run to preview, --uninstall to remove
```

`jetson/tailnet_forward.py` binds only the Jetson's Tailscale IPv4 (never
0.0.0.0), forwards bytes without parsing them and adds no timeout, so HTTP,
WebSockets and the 250 ms stop lease behave exactly as on `:8443`. Idle
connections are kept; a dead peer is dropped by TCP keepalive; if the server is
down the client is closed immediately. The unit is `Restart=always`, independent
of `farsight.service`, and was verified to come back after `systemctl restart`
and after `kill -9`. Check: `curl http://<jetson-tailscale-ip>:8081/health`.

On the operator phone, keep the **Primary rover phone's Tailscale IPv4** set
to the rover phone. To test the direct route as a fallback, enable **Enable
fallback Jetson path** and enter the Jetson's Tailscale IPv4. Stop the bridge
before changing addresses. Fallback is off by default; both routes must reach
the same Jetson server. No Mac upstream or USB port changes are needed.

### Connection selection and recovery

Each new operator TCP connection tries the primary, starting the optional
fallback after 150 ms if no route has won. An immediate primary failure starts
the fallback immediately. Selection has a total 3 s deadline. Only the winning
transport receives application bytes; the losing socket is closed before any
request or control command is read from the client. A failed path is deprioritized
for 5 s for new connections. An existing healthy primary connection can coexist
with newer fallback connections; the UI reports counts for each path.

Once selected, the route stays fixed for that connection. A midstream failure or
3 s blocked write closes it; the next connection can choose another route.
Commands, POST bodies and partial video responses are never replayed. Normal
idle HTTP connections remain open. TCP keepalive detects a dead peer, but a
server that still acknowledges TCP while withholding application replies is
not automatically considered a bad route. The existing control lease and view
freshness guards remain authoritative and may stop driving before transport
failure is detected. This provides connection recovery, not uninterrupted
motion during every single-path failure.

Backgrounding closes every bridge session and stops capture. An explicitly
started **Operator** listener retains its intent only in memory and re-arms on
foreground, with new connections. Camera capture and motion do not auto-resume;
the current Quest reconnect/release gate remains. Manual **Stop bridge**, role
changes and app termination clear that intent. A cold launch requires Start.
Rover capture and its bridge still require explicit restart after backgrounding.

### Evidence and remaining checks

The measured phone chain with #88 reported control RTT p50/p95/max
119.5 / 167 / 228 ms, zero expired permits over 30 s and a maximum permit gap of
108 ms, with H.264 about 9 fps. The [owner's measurement](https://github.com/azizu06/farsight/issues/24#issuecomment-6100097237)
is transport evidence, not proof of motor behavior or complete Quest operation.
Keep the phone-to-phone route primary until the optional direct route has its
own comparable measurements.

Portable macOS Network.framework fixtures exercise refused and delayed primary
connections, zero loser bytes, cooldown, simultaneous primary/fallback sessions,
midstream reset after a POST prefix, blocked writes, both paths unavailable,
normal idle, foreground rearming and manual Stop. Run:

```sh
python3 -m unittest discover -s ios/tests -v
python3 -m unittest tools.tests.test_phone_bridge_chain -v
```

These tests use loopback sockets and a simulated backend. Install the updated
signed app on the operator phone and repeat background/foreground, primary
loss, both-path loss and required-release checks with the actual two phones,
Jetson, Quest and wheels lifted. Record recovery time, chosen paths, video age
and zero-output evidence. Do not replace the primary or declare fault recovery
qualified based only on compilation or portable fixtures.

## Experimental bonded controller and pinned routes

This mode is explicit and **off by default**. It keeps one Quest-to-Mac control
socket while the Mac fans each fresh command over two attached paths to one
logical Jetson owner. A single attached-path loss can preserve control only
while the survivor delivers valid permits and displayed video inside the
unchanged guards. Losing both paths stops immediately and requires release.
The routes share equipment and possibly cellular infrastructure; they do not
cover every outage. See the exact [bonded v1 contract](interfaces.md#opt-in-bonded-upstream-control-experimental-v1).

Prepare the optional direct Jetson forwarder described above. On the operator
phone, stop the bridge and enable **Pinned dual routes for host broker**. This
is mutually exclusive with automatic connection-time fallback. Enter the rover
phone primary address and direct Jetson fallback address, then Start. Wait for
both per-route rows to report ready:

- Phone loopback `8081` forwards only to rover-phone `8081`.
- Phone loopback `8082` forwards only to direct Jetson `8081`.

Neither listener races upstreams or replays bytes. Both addresses are validated
before startup, and either listener bind failure tears down both. An individual
upstream pair failing leaves the other listener available. Operator background
recovery re-arms both; manual Stop clears both and its restart intent.

Use the same USB-selected phone for both Mac mappings:

```sh
python -m tools.phone_usb --dual-route --check
```

The check separately probes GET `/health` on both mappings, reports sanitized
results, and stops its one owned `iproxy` process. It neither opens control nor
proves both origins reach the same backend. Then run:

```sh
python -m tools.phone_usb --dual-route
```

This keeps Mac `18443 → phone 8081` and `18444 → phone 8082` alive, with USB-only
selection and loopback binding. A startup failure cleans up both mappings.
Configure the Jetson server with `--bonded-control` through its normal owner and
service process; do not launch a second process that competes for its camera or
serial port. Finally, on the Mac:

```sh
python -m tools.demo --upstream http://127.0.0.1:18443 \
  --bonded-upstream http://127.0.0.1:18444 --quest
```

The relay privately attaches both paths to the same owner and filters attachment
credentials out of Quest/status/reports. A wrong-backend second route cannot
join the owner or supply its driving image. Each fresh command is sent once to
live routes by independent bounded writers. A blocked writer is retired rather
than delaying the healthy route or creating a command replay queue. Status
ordinals suppress late duplicate statuses. Private `/ws/control/bonded` is not
exposed through the browser proxy. Other newly initiated requests choose one
validated surviving route; mutations such as POST `/scan` are never retried.

The prototype uses redundant **complete JPEG** responses, preserving age/source
headers and normal Quest image decode/display checks. It permits four concurrent
HTTP races with at most eight candidate jobs and 2 MiB per response; excess
requests fail rather than queue. Health reports the actual backend encoder state
plus `relay_transport.profile: "bonded-jpeg"`. Quest disables H.264 while this
profile is active, without overwriting the saved video preference. H.264 and
MJPEG streams are rejected in this mode because stream continuity is not yet
implemented. Redundant JPEG increases cellular traffic; record that overhead
and surviving-path performance before using it on the real link.

A lost path is not automatically reattached in this initial prototype. The
survivor stays active; a new stopped controller session attempts both routes
again. There is no silent new owner after the last-path fault. This limitation
and actual RTT/view margins must appear in the qualification report.

Software evidence includes a canonical simulated backend through two independent
fault proxies, shared-owner continuity through one loss, last-loss zero output,
wrong-backend exclusion, no private welcome exposure, old status/release copies,
blocked writer isolation at 40 Hz, complete-body selection and bounded cleanup.
Phone fixtures separately cover pinned listeners and background recovery. None
proves cellular, Quest or physical motor behavior. Run the independent fault
harness and the four wheels-up checks above before activation. Current unverified
ARKit LiDAR also remains ineligible for physical driving; transport redundancy
cannot remove that separate camera prerequisite.

## Optional USB geometry lane

The geometry lane is separate from camera and control traffic. With the updated
operator bridge app, opt in to Mac `18445 → operator phone 8083`:

```sh
python -m tools.phone_usb --geometry --check
python -m tools.phone_usb --geometry
```

The check requests GET `/geometry/status` on the geometry mapping and GET
`/health` on the control mapping. An open TCP socket alone does not pass the
geometry check. The phone must report geometry protocol 1, a boolean `ready`,
and nonnegative integer anchor, vertex, triangle and dropped counts. If
`ready` is false, start LiDAR mesh capture and keep both bridge apps foreground,
then repeat the check. The report keeps successful control evidence even when
the geometry check fails. It reports geometry readiness/counts separately and
omits capture session identifiers and raw endpoint responses.

`ready` describes the phone listener and its current captured mesh session. It
does not qualify mesh rendering, cellular performance, physical camera timing,
Quest latency or motor control. The geometry helper sends no control commands
and does not change the 250 ms control lease or 500 ms view guard.

To retain the optional pinned control fallback as well:

```sh
python -m tools.phone_usb --dual-route --geometry --check
python -m tools.phone_usb --dual-route --geometry
```

This uses one USB-selected phone and one owned `iproxy` process for
`18443 → 8081`, `18444 → 8082`, and `18445 → 8083`. Every local port is
preflighted before startup; a partial listener or bind failure closes all owned
mappings. All local ports must be distinct. `--geometry-local-port` overrides
18445 only when `--geometry` is enabled. `--check` stops every mapping afterward;
restart without it and keep the helper running for the geometry consumer.
The printed next-step command still configures the camera/control relay;
geometry consumer configuration is a separate step.

The helper's automated evidence uses fake USB tools and real loopback HTTP/TCP
forwarding. Connected-phone USB accessibility and actual ARKit mesh delivery
remain device qualification steps.
