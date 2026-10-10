# Reliability audit — issue #149

This audit addresses reproduced failures in the demo path. Software regression
checks are separate from acceptance of the physical Quest/rover/network rig.
The issue branch and review are [PR #153](https://github.com/azizu06/farsight/pull/153).

| Failure | Repair / evidence |
|---|---|
| A Seek goal changes but an old command can still move the rover | Withdraw the old command; require the exact current goal ID for Goto/Seek |
| Commands, statuses or pending computations survive a sensor reset | Require the current integer generation; recheck after CPU/TF work and before enqueueing |
| A delayed depth encoder reports the sample as newer than it is | Stamp age after encoding; discard expired samples; announce reset before new-world data |
| Initial TensorRT allocation failure disables detection until restart | Retry only TensorRT failures at the existing bounded interval; retain the health error |
| Shutdown frees a detector while inference or rebuild still uses it | Shield and drain active CPU/GPU worker tasks before releasing the detector |
| Private-CA camera succeeds but geometry fails TLS verification | Reuse the configured trust context in the geometry lane; untrusted certificates still fail |
| Fresh Mac decoded IDs keep an old capture replacing the local map | Use the capture's bounded age and the existing stale cutoff, not decode progress |
| Dense integration blocks camera/status publication | Preserve upstream's latest-only worker, 2 Hz / 4096 measured samples per job and independent diagnostics; blocked-worker regressions verify continued capture/navigation publication |
| Old CPU jobs can repopulate a reset dense map | Preserve upstream's cloud replacement; blocked old work cannot mutate the newly published cloud |
| Processing an old pose renews its freshness | Retain receipt time and producer delay instead of using CPU completion time |
| Old phone disconnect cleanup disables a replacement stream | Retain stream ownership until cleanup finishes; withdraw live eligibility immediately |
| Cancelled phone workers publish into a replacement session | Bind pose/RGB-D work to the socket generation and recheck identity under the map lock |
| A reconnect guard also rejects every frame after Reset map | Separate socket authority from the fusion reset counter; real endpoint tests cover both pose and RGB-D continuing after reset |
| Local HTTP 200 hides an unreachable backend, empty geometry or missing dense lane | Add bounded GET-only selected-route checks and exact origin inventory; require matching usable geometry and actual dense GPU drawing when requested |
| A silent geometry WebSocket prevents trying another source and keeps status claiming a live connection | Bound the full handshake and heartbeat writes; require matching Pong replies; withdraw connection authority on error or cancellation. Loopback tests cover silent TCP/WS sources, blocked writes and healthy idle geometry |
| A cancelled session initializer overwrites a new phone, or cancelled cleanup leaves the stream permanently busy | Shield/drain both mutations through repeated cancellation, then release ownership; actual endpoint regressions cover the interleavings |
| Render capture reports geometry but outputs black idle pictures, or drops the tracked pose at the Python/JS field boundary | Integrate upstream's same-task capture draw and field adapter; preserve real-browser pixel tests and add a Node regression executing the actual producer expression and renderer camera/ray code |
| The simulation exports dense data without a usable source identity | Initialize its existing source and recognize only the explicit RGB-D simulation identity with a SIMULATED label; Python-to-JS tests cover initial data, reset and continuation |
| Cinematic camera easing renders an intermediate pose while the frame echoes the latest tracked pose | Draw at the exact accepted pose so Quest's matched-pose placement is valid; preserve cinematic styling. Small head-move/yaw regressions reproduce the mismatch under default settings and `camera=smooth` |
| Old camera decoding changes a replacement socket's upside-down mount state | Check the current binding while updating mount hysteresis; recheck publication after rotating pixels |
| Upright person boxes reuse unrelated depth just because their frame number matches | Retain native JPEG and a server-created capture key; match the exact socket/capture and source before projecting markers |
| Seek projects upright or delayed detections into the newest unrelated native depth | Retain 12 exact captures and use the detector input's source, socket, frame and orientation. Recheck after projection; invalid pairing withdraws the old goal, while delayed valid inference uses its retained measured capture |
| Simulated Seek has no capture metadata, or withdraws a valid target while new simulated depth is still pending | Publish simulated boxes only after the exact depth reaches observers, retaining the previous accepted packet and its original age during pending work |
| Packed stereo without a matching eye pair falls back to a double-image mono picture | Require actual matching eye placement for packed segments; invalid layout or missing pair returns to the local map. Actual-class tests cover pair recovery and stale captures |
| Fast real-camera inference discards a valid Seek goal before its new depth reaches Nav | Retain the last successfully projected input without renewing its age. Preserve its goal only while the exact newer input is proven by the current camera, the target remains visible, and the prior measured input remains fresh; update from new depth when accepted |
| A camera request counts preview/executor residence twice and stops on a fresh capture | Report completed handler wait separately; subtract only validated wait from the client request interval. Actual-route and actual-class tests preserve old captures, duplicate expiry and all transfer/decode/render delay |
| A five-view scan competes with camera publication in a long Python voxel arithmetic loop | Vectorize cell averaging while preserving every accepted sample, observation weight, array ownership, order and capacity. Deterministic equivalence checks cover collisions, eviction and reset |
| Later release messages erase the initiating scan failure | Preserve the first scan stop cause; optional bounded judge traces distinguish capture age, HTTP time, display and control without recording permits |

The frame and motion age cutoffs are preserved. Repairs do not grant face
recognition or model text authority over motors, synthesize metric geometry, or
quietly change production routes.
The browser judge check now samples continuous motion through three seconds of
held manual drive and six seconds of Explore, including forward/turn phases,
then verifies release STOP. This is a local simulation check, not a physical
speed limit or evidence of venue-network timing.

## Deployment

Use the same revision for the Python navigation server and rebuilt ROS bridge;
see [navigation.md](navigation.md#server-and-bridge-protocol-wsnav-version-1).
An old bridge lacks mandatory command/status generations and cannot drive the
updated server. This intentional hold must not be diagnosed as joystick loss.

The checked-in production service (`jetson.launch`) and `tools.demo --simulate`
launch Python. The separate `farsight-server` Rust backend is not qualified as
an equivalent replacement by its workspace test count. Before switching the
rover backend to Rust, port and regress the current socket-generation authority,
capture/depth age after queueing and encoding, reset-before-depth ordering, and
exact camera/native-JPEG/source association for upright detections and LiDAR
lookups. Neither backend currently reports an explicit runtime or build revision
in `/health`; record the actual launch command and deployed revision at the
selected backend origin before claiming parity.

For the selected production topology and bounded route checker, see
[demo-connectivity.md](demo-connectivity.md) and
[macview-rig-acceptance.md](macview-rig-acceptance.md). The renderer is currently
on the operator Mac's port 8091. Use the exact qualified backend origin from the
actual running relay; a commissioning address is not an accepted substitute.
A healthy render encoder with zero drawn geometry does not qualify a demo.

## Remaining physical acceptance

- Quest immersive pixels, decoded cadence, capture age, head motion and controller
  release/held-trigger behavior on the actual shipped build.
- Supervised continuous drive, STOP, unplug/link loss and physical Uno watchdog
  timing, followed by a fresh release/press before recovery.
- Matching ROS overlay deployment, goal replacement, same-identity phone
  reconnect, tracking loss/recovery and measured obstacle behavior.
- Production USB/cellular/Tailscale chain at the venue under full simultaneous
  camera, voice, detection and mapping load; no venue Wi-Fi dependency.
- Memory/thermal headroom with the actual concurrently loaded models. CPU fixtures
  and model weights alone do not measure Jetson runtime headroom.

Raw ROS Twist does not carry a goal ID. Pending/reset/failed goals are gated,
but a delayed old Twist arriving after a replacement goal has been accepted
cannot be conclusively attributed. This remains a ROS stack qualification limit.
Face enrollment/identity still needs consenting-volunteer and camera-mount checks;
face scoring and name-directed navigation remain separately gated work.

## Reproduce the software checks

Install the declared Python/ROS test dependencies in the selected interpreter.
In particular, voice upload tests need `python-multipart`, dense compression
checks need Brotli, and child ROS smoke scripts need PyYAML in their interpreter.
The standalone renderer browser fixture also needs Chromium and FFmpeg; its
software renderer and CPU encoder do not qualify Mac VideoToolbox performance.
Do not classify a missing package as a model performance failure.

```sh
python -m pytest jetson/tests jetson/control/tests jetson/mapping/tests tools/tests ios/tests rover/tests tests -q
npm --prefix quest test
npm --prefix quest run build
npm --prefix quest run test:browser
npm --prefix quest run test:judge
cargo test --workspace --manifest-path rust/Cargo.toml
```

The browser/judge harness uses explicit simulation/replay; it is not a physical
motion test. ROS callback tests stub ROS transport and verify callback authority;
they do not replace rebuilding and testing the real overlay.
