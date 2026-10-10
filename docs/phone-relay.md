# Phone-owned cellular relay

The confirmed design puts the cellular connection on the two iPhones. The Mac
and Jetson use local USB connections. **Implementation and hardware qualification
are in progress; the computer-side Tailscale path remains a fallback.**

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
