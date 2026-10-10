# Mac visual offload (prototype)

The Mac renders the 3D map and hardware-encodes it. The Quest only decodes and
shows the picture. This path never sends `/ws/control` and never moves the rover.

**What this is:** a flat, pose-driven view. The camera follows the head.
**What this is not:** stereo VR. Nothing here is labelled stereo.

Every number below is **Mac desk, simulated map, no headset** unless a line says
otherwise.

## Run it

Playwright's own headless shell is not installed on this desk. Pass a Chromium:

```
python -m tools.mac_render --sim-world --port 8090 \
  --browser "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

Open `http://127.0.0.1:8090/`. That page is the viewer, served by the Mac, and
both sockets stay on that origin. The corner shows the frame age once a
fragment arrives.

A page hosted somewhere else still works if you name the Mac:

```
http://127.0.0.1:8092/macview.html?mac=http://127.0.0.1:8090
```

`?mac=` moves the video socket and the pose socket. A page with no `?mac=`
keeps both sockets on its own origin.

On the headset, reverse the Mac port, then open the page:

```
adb reverse tcp:8090 tcp:8090
```

## Message shapes (for review, not bound yet)

`/ws/macview` uses the same request/fragment pattern as `/ws/video`, so the
existing H.264 player can play it. `type` matches that player. `macview` names
the offload message.

Init, then the fMP4 init bytes:

```json
{"type":"video_init","macview":"macview_init","version":1,
 "mime":"video/mp4; codecs=\"avc1....\"","source":"MAC RENDER",
 "width":1280,"height":720,"fps":30,"stereo":false,"encoder":"h264_videotoolbox"}
```

Viewer asks `{"type":"next","request_id":1}`. Server answers with JSON, then
the fMP4 fragment bytes:

```json
{"type":"video_segment","macview":"macview_segment","version":1,"request_id":1,
 "age_ms":221,"frame_id":12,"frames":3,"source":"SIMULATED","stereo":false,
 "media_start_s":0.0,"media_end_s":0.1,
 "capture_mono_ms":1200.5,"capture_wall_ms":1710000000000.0,
 "pose":{"sent_mono_ms":1180.0,"sent_wall_ms":1709999999980.0,"received_mono_ms":1182.0},
 "encode_ms":220,"queue_ms":0.03,"render_ms":0.1,"capture_ms":0.8}
```

`capture_wall_ms` is the oldest frame inside the fragment, not the newest. A
fragment holds 3 frames, so reporting the newest one made the picture look
about 67 ms fresher than it was. `encode_ms` is the time from handing that
oldest JPEG to VideoToolbox until the fragment comes back. It includes the
encoder's own queue, about 6 frames on this desk. `queue_ms` is only the wait
before the JPEG is handed over, and it is under 1 ms.

`/ws/macview-pose`, viewer to Mac, at most 72 Hz:

```json
{"type":"pose","version":1,"sent_mono_ms":12.5,"sent_wall_ms":1710000000000,
 "head":{"position":[0,1.6,0],"quaternion":[0,0,0,1]},
 "ray":{"origin":[0.2,1.4,-0.3],"direction":[0,0,-1]}}
```

`ray` is the right controller, matched by hand, or null. A pose the Mac has
held longer than 250 ms is dropped. Both ends of that check are the Mac
monotonic clock. The viewer's clock is never compared with the Mac clock.
That bound is local to this view. It does not change any motor lease.

The pose echoed in a fragment is the pose the render page had already drawn,
not a pose that arrived after the picture was taken.

Frame age is `now - capture_wall_ms`. Pose-to-frame is
`capture_mono_ms - pose.received_mono_ms`, both on the Mac clock.

## Encode

`h264_videotoolbox` with `-realtime 1 -bf 0 -g 30 -profile:v main`. If
VideoToolbox is missing, it falls back to `libx264 -preset veryfast -tune
zerolatency`. Fragments match `jetson/video.py`: `frag_keyframe+empty_moov+default_base_moof`,
timescale 1,000,000, 100 ms fragments.

## Measurements

```
python -m tools.mac_render.bench --upstream http://127.0.0.1:8090 --out docs/mac-offload-bench.json
```

The committed numbers are in [mac-offload-bench.json](mac-offload-bench.json).

### This desk, 2026-10-10

Apple silicon, `h264_videotoolbox`, simulated map (`--sim-world`), 6 seconds.
**Mac desk, simulated map, no headset.**

| Point | Render p50 | Capture p50 | Encode p50 | bytes/s | Frame age p50/p95 | Pose-to-frame p50/p95 |
|---|---|---|---|---|---|---|
| 1280x720@30 | 0.10 ms | 0.81 ms | 220 ms | 769,206 | 221 / 251 ms | 23.4 / 66.5 ms |
| 1920x1080@30 | 0.10 ms | 0.87 ms | 212 ms | 789,011 | 213 / 249 ms | 27.4 / 66.5 ms |

Render p50 is the Three.js draw only. Capture p50 is the screenshot reaching
Python. Encode p50 is VideoToolbox, JPEG in to fragment out, including its
queue. Frame age is the oldest frame in the fragment. Pose-to-frame is the
pose that was drawn, not a pose received after the shot.

The lit look and the old unlit look draw in the same 0.10 ms at 720p on this
desk. The two pictures are
[before](assets/mac-offload-before.png) and
[after](assets/mac-offload-after.png). Same simulated map. The lit one uses
vertex normals, a matte surface, one key light and a hemisphere fill. It does
not invent geometry.

## Still needs the real Quest

- Decode time and dropped frames on the Quest Browser decoder.
- Quest Browser's real max resolution and frame rate for a WebXR quad.
- Comfort: a flat pose-driven picture is not stereo. Stereo needs side-by-side
  eyes from the XR view offsets, and only after the flat path is measured.
- Pose-to-photon on the headset. The Mac number stops at encode.
