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

The frame and motion age cutoffs are preserved. Repairs do not grant face
recognition or model text authority over motors, synthesize metric geometry, or
quietly change production routes.

## Deployment

Use the same revision for the Python navigation server and rebuilt ROS bridge;
see [navigation.md](navigation.md#server-and-bridge-protocol-wsnav-version-1).
An old bridge lacks mandatory command/status generations and cannot drive the
updated server. This intentional hold must not be diagnosed as joystick loss.

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
