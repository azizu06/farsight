# Read-only preflight for the 6:45 AM hardware handoff

Run this on the MacBook and again on the booted Jetson. It inventories the
current machine; it does not qualify the real demo, configure the rig, or imply
that hardware is connected because a Python test passes.

```sh
python -m tools.preflight --role operator > /tmp/farsight-mac-preflight.json
# On the Jetson, from the repo and its intended venv:
python -m tools.preflight --role rover > /tmp/farsight-jetson-preflight.json
```

`auto` selects the rover role when a Jetson device-tree/L4T marker exists,
otherwise operator on macOS and both on other systems. The report includes the
actual Python interpreter. Subprocess probes have 1.5-second timeouts, with a
3-second limit for Python imports and the canonical CLI. Independent checks run
in parallel. Exit zero means a report was collected. `--strict` exits one for
failed or unknown local checks; even strict success does not establish physical
qualification. Missing/unknown versions are `null`, never a made-up zero.

The helper does not install packages, use sudo, flash firmware, open a motor
serial port, start a server, capture camera frames, configure ADB reverse, log in
to Tailscale, modify networking, test arbitrary peers, or scan networks. An
already-running **local** ADB daemon is queried over its documented host
protocol at `127.0.0.1:5037`; the helper avoids `adb devices` because that command
can automatically start the daemon. If no daemon is running, ADB device state
is unknown. A human can run `adb devices` before repeating the inventory.

## What the JSON proves

- `python`: the selected interpreter can import the required modules for its
  role, reports actual module versions, and compares them with the repository's
  declared requirement bounds. Out-of-range versions fail explicitly, even if
  the import works. Resolve failed imports/version mismatches using the
  documented requirements before going offline; the helper does not alter the
  current environment.
- `ffmpeg`: FFmpeg is installed and its encoder list contains `libx264`, which
  the video service explicitly requests. This does not prove live H.264
  latency, throughput, or successful camera encoding.
- `gpu`: on the rover, TensorRT's import version, a CUDA runtime version query,
  CUDA compiler version when installed, L4T
  marker, and available package metadata. It does not enumerate CUDA devices,
  execute engines, or assume a missing compiler means no GPU exists.
  Operator GPU checks are marked skipped.
- `camera` and `serial`: visible device paths and optional V4L2 device inventory.
  A serial filename is a candidate, not an identified Uno or verified firmware.
  Phone camera streaming requires the native app; a hotspot alone is not a
  camera source.
- `network` and `tailscale`: local interfaces/routes or macOS network services,
  installed CLI state, and the Tailscale backend's reported authentication state.
  Running means the backend is authenticated; starting/stopped/error states can
  remain unknown. No direct/relay route or available cellular capacity is
  established by this report.
- `adb`: installed version and states of attached devices from an existing
  daemon. `device`, `unauthorized`, and `offline` are distinct. An authorized
  Android device is not automatically identified as the Quest. Verify it
  physically, select its serial if necessary, and test immersive WebXR.
- `models`: each `*.manifest.json` model's actual SHA-256 and byte count against
  the prepared export manifest. Missing or mismatching files fail. Other local
  weights are listed by size with integrity unknown, because no trusted
  manifest was supplied. A matching ONNX hash does not prove engine execution,
  detector accuracy, or license compliance.
- `server_cli`: `python -m jetson.server --help` imports and exposes the expected
  current flags without starting the application lifespan. `jetson.server` is
  the canonical launcher; `jetson.mapping.server` remains compatible.

Model weights have their own provenance. The report preserves each manifest's
`license` and `source` fields without inventing a license decision. Current YOLO
manifests state AGPL-3.0 or an applicable Ultralytics license. The optional face
model's source/license discrepancy is documented in `models/README.md`. Code
written for Farsight and third-party pretrained weights are separate artifacts.

## Resolve the handoff in this order

1. Confirm the Jetson boots independently and note its actual L4T, Python,
   CUDA/TensorRT versions. Use JetPack system Python or the documented
   `--system-site-packages` venv; do not substitute a desktop CUDA wheel.
2. Confirm prepared model/manifest pairs copied intact. Build TensorRT plans
   on that Jetson and run the zero-input smoke test, then real camera validation
   from `docs/inference-setup.md`. No GPU hardware was qualified by CPU mocks.
3. With wheels raised, identify the Uno port manually and perform every bench
   gate in `rover/README.md`: firmware handshake, direction, range, deadman,
   independent watchdog, USB disconnect, and calibrated slow movement. The
   preflight deliberately does not flash or open the port.
4. Connect each phone to its computer by a data cable and approve trust. Confirm
   the USB interfaces and routes. Enter the **Jetson USB interface IP**, not the
   phone's gateway IP, in the native capture app. Verify `/health` identifies
   fresh `PHONE RGB` or `IPHONE LIDAR` frames.
5. Confirm Tailscale is installed and authenticated on both computers. Verify
   the configured peer and direct/relay transport manually, then run
   `tools.qualify_link` with the Quest page closed. The qualifier sends releases
   only; it does not move motors or prove camera-to-display latency.
6. Connect and unlock the actual Quest, approve USB debugging, apply the
   documented ADB reverse, and open the MacBook localhost origin. Verify WebXR
   inside the headset and physically measure displayed video age.
7. Rehearse full loaded operation, cable loss, stalled video, stale controls,
   reconnection requiring a new press, power/temperature stability, and cold
   boot with venue Wi-Fi disabled before floor driving.

Canonical camera-service launch, initially with **physical motors disabled**:

```sh
python -m jetson.server --host 0.0.0.0 --port 8443 --h264
```

Select `--serial-port` only after the separate hardware checks pass. The report's
`physical_qualification.complete` always remains false: it intentionally cannot
certify motor behavior, phone routing, cellular capacity, Quest rendering, or
physical stop times from this machine's software inventory.

Helper policy tests:

```sh
python -m unittest discover -s tests -p test_preflight.py -v
```
