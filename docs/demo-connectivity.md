# Production demo network architecture

This is the short network contract for the demo. The complete phone setup and
qualification procedure is in [phone-relay.md](phone-relay.md). **The
phone-owned cellular route below is the primary path. Venue Wi-Fi, Quest Wi-Fi,
and MacBook/Jetson Wi-Fi are not part of the Farsight app path.**

```text
Quest Browser, http://localhost:8080
  └─ USB-C + ADB reverse
     MacBook operator relay, 127.0.0.1:8080
       └─ USB + iproxy, localhost:18443 → phone localhost:8081
          Operator iPhone, Farsight Capture
            └─ cellular + Tailscale
               Rover iPhone, Farsight Capture :8081
                 └─ local USB network
                    Jetson :8443
                      └─ USB serial → Arduino Uno → drive motors
```

The Quest stays cabled to the MacBook for the demo. ADB reverse forwards the
local page over USB; the Quest opens only `http://localhost:8080`, and its HTTP
and WebSocket requests stay same-origin through the Mac relay. Do not point the
browser at a Jetson IP, tailnet address, hotspot address, or venue-LAN host.

The operator and rover phones run their Farsight bridge roles and use cellular
data with Tailscale between them. The Mac reaches the operator phone by USB
`iproxy`; the rover phone reaches the Jetson over its local USB network. The
rover phone's Personal Hotspot setting may be needed to bring up that **cabled**
USB network; it is not a wireless Quest-to-Jetson path. Raw camera/depth stays
on the rover phone–Jetson USB link. Processed video, map, status and controls
cross the phones' cellular/Tailscale connection.

## What has and has not been measured

The [owner's live-chain report](https://github.com/azizu06/farsight/issues/24#issuecomment-6100097237)
records both phones' Wi-Fi off and the Mac–Jetson commissioning cable unplugged.
It reports control RTT p50/p95/max of 119.5/167/228 ms, zero expired permits in
30 seconds, a 108 ms maximum permit gap, and about 9 fps H.264. This is useful
cellular transport evidence. It does not qualify Quest display age, continuous
physical driving, link-fault recovery, or a 15-minute full-load run. Loopback
fixtures and direct tailnet pings are not substitutes for those checks.

## Demo go/no-go

Before putting the headset on, check the selected routes on the operator Mac:

```sh
python -m tools.check_demo_routes --operator http://127.0.0.1:8080 \
  --require-dense --require-render --renderer http://127.0.0.1:8091 \
  --output /tmp/farsight-demo-routes.json
```

Add `--expected-upstream` with the exact previously qualified Jetson route to
reject an accidental switch to a commissioning tailnet address. Add
`--expected-geometry` only when that separate lane is intentionally selected.
This command makes bounded GET requests only. It reports the actual selected
backend, geometry and renderer origins; requires fresh drive-ready camera/rover
status, usable matching geometry, a populated dense accumulator and positive
dense GPU draw/upload counts. A 200 response with zero or disconnected geometry
fails. `/dense/status` returning 404 means this relay has no dense accumulator;
start the owner-selected `tools.dense_map` launcher, or `tools.demo --dense-map`.
`tools.dense_map` itself does not accept a `--dense-map` flag.

For a camera/control rehearsal before scanning, use `--camera-only` without
`--require-dense`/`--require-render`. It still requires a drive-ready camera and
connected rover. The checker does not prove headset pixels, latency, physical
watchdog behavior or route performance under load; complete the following checks
on the actual rig. Keep the required Python packages installed in the launch
environment, including `python-multipart` for voice uploads and PyYAML for ROS
tools. An incomplete test or launch environment is not a model-performance result.

1. Keep the Quest–Mac USB cable connected. On the Mac, `adb devices` must show
   the Quest as `device`; `adb reverse --list` must show port 8080. Load only
   `http://localhost:8080`.
2. Start both phone bridge roles and verify the Mac→operator phone USB forward,
   operator→rover phone Tailscale/cellular hop, and rover phone→Jetson USB link.
   Use the Jetson's actual USB-interface IP; `172.20.10.3` is only the last
   observed address, not a fixed assignment.
3. For the rehearsal, disable Wi-Fi on the Quest, MacBook and Jetson. Keep phone
   cellular enabled. Confirm fresh live camera, detections and map are visible
   in Quest, with no direct headset connection to the backend.
4. Run video, telemetry and the stopped control session together for 15 minutes
   at the demo location. Record path type (direct/DERP), control RTT, displayed
   frame age, decoded FPS, dropped frames and throughput. Do not infer Quest
   frame age from server camera age.
5. With wheels raised, interrupt either phone's cellular service, each USB
   connection, and each bridge app. Confirm the documented 250 ms Jetson lease,
   300 ms Uno watchdog and 500 ms displayed-frame guard cause a physical stop;
   measure them, then confirm recovery requires a fresh release and press.
6. If the cellular route is unavailable, stop and use clearly labelled replay
   or simulation with motors disabled. Do not silently switch to venue Wi-Fi or
   continue driving on stale video.

There is no honest way to guarantee zero outages. This topology removes campus
Wi-Fi as a failure source and makes link loss fail safe; cellular coverage,
carrier routing, USB cables and foreground phone apps still require the rig
rehearsal. The optional direct Jetson tailnet route is a candidate fallback, not
the primary route and not accepted until it passes the same end-to-end checks.
The separate geometry lane is still being integrated; it must use an explicitly
tested USB or cellular/Tailscale route and must not add a hidden venue-Wi-Fi
dependency.

The older computer-side Tailscale/USB-tether procedure is documented separately
in [transport-setup.md](transport-setup.md) as a fallback only. It does not
replace or qualify this primary phone-owned route.
