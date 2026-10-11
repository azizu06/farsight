# Mac point capacity and live link witness

Measurements on 2026-10-10, Apple M5 Pro / 48 GiB, Chrome, ANGLE Metal and
`h264_videotoolbox`. These are synthetic desktop measurements, not captured room
detail or Quest frame rate. The live :8091 renderer remained running concurrently.

```sh
python -m tools.bench_mac_capacity --output .cache/mac-capacity.json
```

The fixture uses production dense state/GPU chunks and `RenderEncoder`, unique
centimetre cells, small round points, 97,410 textured triangles, full geometry,
1920×1080 SBS at native pixel ratio, and a requested 30 fps. Only its served
module's capacity validation is raised to permit the 10M probe; production
renderer limits are owned separately. Both eyes submit all requested points.

| Unique points | GPU chunks | Encoded fps | Picture age p95 | Capture evaluate p50 | Stereo raster p50 / p95 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000,000 | 123 | 7.85 | 878 ms | 111 ms | 0.30 / 1.21 ms |
| 2,000,000 | 245 | 14.19 | 476 ms | 63 ms | 1.30 / 2.81 ms |
| 5,000,000 | 611 | 8.74 | 675 ms | 85 ms | 5.15 / 79.0 ms |
| 10,000,000 | 1,221 | 7.90 | 819 ms | 105 ms | 6.20 / 12.15 ms |

The raster probe uses synchronous stereo draws plus `gl.finish()`, without JPEG
readback. Ten million points uploaded and drew successfully. This is a tested
capacity lower bound, **not a measured 30 fps encoded ceiling**. None of these
full pipelines achieved 30 fps; even the live ~47k cloud encoded ~22.5 fps before
the heavy fixture. Non-monotonic timings and the 5M raster spikes reflect shared
load. Capture/evaluation, not a 100k GPU limit, is the demonstrated immediate
bottleneck. Retention is raised to 10M independently of the encoder work.

## Caps between capture and pixels

| Stage | Current / selected bound | Limiting resource or purpose |
| --- | --- | --- |
| Phone measured RGB-D | Native valid depth/confidence pixels; no synthesized points | Sensor resolution, tracking and capture |
| Jetson navigation cloud | 120k cells, 4 cm | Jetson CPU/navigation geometry; separate from visual history |
| Jetson coarse wire | Default 30k points; deployed triangle cap 12k | Bulk egress; next approved window proposes zero visual decimation, retaining the same 120k nav pool |
| Jetson fine cache | 600k cells, 1 cm, 30 s TTL | Source CPU/RAM; expiry tombstones do not delete Mac historical observations |
| DenseWorker | Deployed 2 Hz × 4096; retain this rate in next window, CPU budget 0.15 | Existing Jetson GIL/camera stalls; faster input held by Firstmate051 pending stride-4 and camera-p95 proof |
| Mac retained history | 10M unique cells, retained within capture epoch | Highest upload/draw capacity tested; actual source identity resets still clear |
| Mac GPU receiver | Owner's separate capacity setting; must match retained ceiling | 10M tested in isolated fixture; update/deployment requires renderer owner |
| Mac full detail API | 8192 records/page, incremental cursor; no total-view LOD cap | Bounded local transfers; `/dense/delta.bin` can traverse all retained cells |
| Quest direct geometry fallback | 32 spatial chunks × at most8192 points, distance LOD | Quest frame budget; Mac GPU capacity is not evidence that Quest2 can draw10M at72fps |
| Mac video | 1920×1080 SBS, requested30fps/4Mbps | JPEG/readback/capture pipeline currently below target |
| Raw textured layer | Networking OFF by default; one native frame when enabled | Separate compressed-link and camera qualification; no history-sized transfer |

## Live network measurement

```sh
python -m tools.measure_dense_budget --origin http://100.103.169.40:8081 \
  --output .cache/dense-link.json
```

This performs GETs only. Repeated cached measured pages are traffic, never fused
as new points. It records server video-frame progress, camera age and control
mode without sending any motor/control command. Use `--path` with an existing
static asset to measure the unpaced link separately from the source bulk limiter.

The deployed aggregate bulk limiter was **1.5 Mbps**, with video unpaced. A gzip
dense-page probe added only 0.34–0.37 Mbps despite requested0.5–5 Mbps; page-p95
latency reached1.85–2.20 s. This is governed bulk availability, not the cellular
link's maximum. A separate existing static-asset probe sustained4.86 Mbps added
traffic at a5 Mbps request and19.64Mbps at a20Mbps request. Server video encoded8.52fps versus8.33fps baseline in the5Mbps window;
camera stalls existed in baseline too (camera age p95~538ms, max594ms).

Every observed control mode was teleop/deadman released. This **does not qualify
AUTO stability**, decoded camera performance, a higher DenseWorker deployment,
or the maximum link. Raw pulls remained zero. Keep input2Hz/4096 and raw off
until the coordinated deployment and fresh camera/AUTO witness.
