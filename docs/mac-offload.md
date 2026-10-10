# Mac visual offload (prototype)

The Mac renders the 3D map and hardware-encodes it. The Quest only decodes and
shows the picture. This path never sends `/ws/control` and never moves the rover.

**What this is:** a flat, pose-driven view. The camera follows the head.
**What this is not:** stereo VR. Nothing here is labelled stereo.

Every number below is **Mac desk, no headset** unless a line says otherwise.

## Run it

```
python -m tools.demo --simulate          # map source, another terminal
python -m tools.mac_render --upstream http://127.0.0.1:8080 --port 8090
```

Or one process, simulated map included:

```
python -m tools.mac_render --sim-world --port 8090
```

Open `http://127.0.0.1:8090` is the encoder. The viewer is the Quest page
`macview.html` (built into `quest/dist/macview.html`). Point it at the encoder
with `?mac=http://127.0.0.1:8090`.

On the headset, reverse both ports, then open the page through the relay:

```
adb reverse tcp:8080 tcp:8080
adb reverse tcp:8090 tcp:8090
```

## Message shapes (for review, not bound yet)

`/ws/macview` uses the same request/fragment pattern as `/ws/video`, so
`quest/src/h264.ts` can play it. `type` matches that player. `macview` names
the offload message.

Init, then the fMP4 init bytes:

```json
{"type":"video_init","macview":"macview_init","version":1,
 "mime":"video/mp4; codecs=\"avc1....\"","source":"MAC RENDER",
 "width":1280,"height":720,"fps":30,"stereo":false}
```

Viewer asks `{"type":"next","request_id":1}`. Server answers with JSON, then
the fMP4 fragment bytes:

```json
{"type":"video_segment","macview":"macview_segment","version":1,"request_id":1,
 "age_ms":40,"frame_id":12,"source":"SIMULATED","stereo":false,
 "media_start_s":0.0,"media_end_s":0.1,
 "capture_mono_ms":1200.5,"capture_wall_ms":1710000000000.0,
 "pose":{"sent_mono_ms":1180.0,"sent_wall_ms":1709999999980.0,"received_mono_ms":1182.0},
 "encode_ms":3.1,"render_ms":6.4,"capture_ms":1.2}
```

`/ws/macview-pose`, viewer to Mac, at most 72 Hz:

```json
{"type":"pose","version":1,"sent_mono_ms":12.5,"sent_wall_ms":1710000000000,
 "head":{"position":[0,1.6,0],"quaternion":[0,0,0,1]},
 "ray":{"origin":[0.2,1.4,-0.3],"direction":[0,0,-1]}}
```

`ray` is the right controller, or null. A pose older than 250 ms on the Mac
clock is not applied. That bound is local to this view. It does not change any
motor lease.

Frame age is `now - capture_wall_ms`. Pose-to-frame is
`capture_mono_ms - pose.received_mono_ms`, both on the Mac clock.

## Encode

`h264_videotoolbox` with `-realtime 1 -bf 0 -g 30 -profile:v main`. If
VideoToolbox is missing, it falls back to `libx264 -preset veryfast -tune
zerolatency`. Fragments match `jetson/video.py`: `frag_keyframe+empty_moov+default_base_moof`,
timescale 1,000,000, 100 ms fragments.

## Measurements

```
python -m tools.mac_render.bench --upstream http://127.0.0.1:8090
node tools/mac_render/bench.mjs --upstream ws://127.0.0.1:8090
```

Target points: 1280x720@30 and 1920x1080@30. Report render ms, capture ms,
encode ms, bytes/s, frame age p50/p95, pose-to-frame p50/p95.

### This desk, 2026-10-10

Apple silicon, `h264_videotoolbox`, simulated map (`python -m tools.mac_render --sim-world`),
6 seconds, ~124 fragments. **Mac desk, no headset.**

| Point | Render p50 | Capture p50 | Encode p50 | bytes/s | Frame age p50/p95 | Pose-to-frame p50/p95 |
|---|---|---|---|---|---|---|
| 1280x720@30 | 0.10 ms | 0.88 ms | 0.005 ms | 360,658 | 2.1 / 4.9 ms | 47.3 / 55.9 ms |
| 1920x1080@30 | 0.10 ms | 1.08 ms | 0.007 ms | 588,122 | 4.2 / 7.5 ms | 45.4 / 51.9 ms |

Encode p50 is the time to stamp the fragment, not the encoder's internal queue.
Pose-to-frame is pose received on the Mac to the captured frame. It is not
pose-to-photon on a headset.

Label on every row: **Mac desk, no headset**.

## Still needs the real Quest

- Decode time and dropped frames on the Quest Browser decoder.
- Quest Browser's real max resolution and frame rate for a WebXR quad.
- Comfort: a flat pose-driven picture is not stereo. Stereo needs side-by-side
  eyes from the XR view offsets, and only after the flat path is measured.
- Pose-to-photon on the headset. The Mac number stops at encode.
