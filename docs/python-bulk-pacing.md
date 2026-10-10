# Python source bulk pacing: modeled-link check

This is a local software simulation on macOS, using production Python services,
the operator relay, and `tools.fault_proxy`. No Jetson access, physical camera,
phone, headset, or motor hardware was used. The rover was explicitly simulated.
The workload included the real CPU YOLO pose ONNX model and real FFmpeg H.264,
with generated RGB input, 500 kbps / 10 fps video, a 100 ms modeled round trip,
and 2 Mbps shared link capacity. The 250 ms lease and 500 ms view guard remained
unchanged.

One valid synthetic 200,000-point snapshot was requested over compressed HTTP
while video and control ran. Its raw size was 3,200,004 bytes and gzip size was
2,961,734 bytes. This is a sizing fixture, not a reconstructed real scene. Each
paired trial lasted twelve seconds; neither large download finished during that
window.

| Bulk limit | Scenario | Source bulk handoff rate | Control ACK p95 | Capture-receipt age p95 |
|---|---|---:|---:|---:|
| Disabled | Snapshot + video | 2.377 Mbps | 193 ms | 375 ms |
| 1.4 Mbps | Snapshot + video | 1.389 Mbps | 163 ms | 432 ms |
| Disabled | Abort snapshot at 3 s | 2.373 Mbps overall | 153 ms | 366 ms |
| 1.4 Mbps | Abort snapshot at 3 s | 0.649 Mbps overall | 152 ms | 424 ms |

The source cap worked and the snapshot pair's control acknowledgement improved.
Capture-receipt age increased with pacing, so this is a latency tradeoff, not a
general link qualification. Guarded motion did not sustain in either snapshot
trial. The paced snapshot trial included an accepted permit aged 249.8 ms, which
expired immediately afterward and correctly latched a stop. Video sockets
remained connected in all four trials. No runtime change was made in response to
these results; the regression was reported to the integration owner first.

Cancellation released the shared budget. In the paced abort trial, only one
additional 4,375-byte map chunk was handed off after the local client cancelled.
Over the next nine seconds the receiver obtained ninety further video fragments.
The final pacer pending count was zero and measured future debt was zero. Video
age at the final observation was 345 ms. A stopped deadman did not automatically
rearm merely because the picture recovered.

The primary `/ws/map?points=http` subscription acknowledged HTTP point transport
and sent no binary map packets in every trial. Gzip survived the production relay:
a separate short metadata probe verified `content-encoding: gzip` and raw bytes
`1f8b`. The initial helper copied headers into a case-sensitive dictionary and
incorrectly recorded a null encoding; that accounting bug is preserved and
explained in the evidence rather than represented as a transport failure.

Source byte rates count successful ASGI body/binary handoffs, which can include
buffered bytes awaiting the modeled wire. The fault proxy's measured aggregate
wire rate stayed below 2 Mbps. These measurements do not prove cellular behavior,
camera-to-eye latency, actual Jetson throughput, or physical stop timing. Pose
inference was active with no model error; its observed processing rate was around
10 fps on this generated scene, not an accuracy evaluation.

Evidence: [python-bulk-pacing.json](qualification/python-bulk-pacing.json).
Reproduce on a quiet software host:

```sh
python -m tools.qualify_python_bulk --model models/yolo11n-pose.onnx --report docs/qualification/python-bulk-pacing.json
```

The helper starts only isolated simulated services, generates the sizing packet,
and cleans up its own processes. Firstmate's `tools/budget_link.py` and the shared
qualification report are unchanged.
