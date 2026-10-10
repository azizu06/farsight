# Video quality

Laptop simulation, 2026-10-10. Not the rig, not the cellular link, not a headset.

The rig shows JPEG unless someone turns H.264 on. `FARSIGHT_H264=0` in
`jetson/farsight.env.example`, and the viewer defaults to `h264: false`
(`quest/src/viewer-settings.js`). When `/health` says the rover offers H.264
and the operator has not chosen, the viewer switches to it (`defaultH264`).
An explicit JPEG choice still wins. `/camera.jpg` is unchanged in both
profiles: native capture already sends 640 px quality-0.5 JPEGs, and the
native LiDAR producer caps fresh video at 6 fps.

## Link budget

Measured operator link: a DERP-relayed tailnet at about 3 Mbit/s effective.
Map downloads on that link already starved control past `LINK_LOST_MS` 500.
PR #124 gave live video its own unpaced lane (`FARSIGHT_VIDEO_LIMIT_MBPS`,
default 0 = unpaced), separate from map bulk. Video still has to leave room
for control, detections and health, which have to stay under 500 ms.

`low` is 500 kbps, about a sixth of the link. `hq` targets 1.5 Mbps, half the
link before the rest of the traffic. `hq` stays opt-in until the real path is
measured. These laptop numbers are not that measurement.

## JPEG note for the preview owner

Mehdi measured the frontend at up to 10 JPEG requests/s, not the old 3–6
effective fps. Budget the preview at that rate:

| JPEG size | 10 req/s | vs ~3 Mbit/s DERP link |
|---|---|---|
| 20 kB | 1.6 Mbit/s | about half the link, before control, health, or map |
| 60 kB | 4.8 Mbit/s | over the link by itself |

So the low JPEG settings stay exactly as today in every profile: `preview()`
still caps width at 640 and quality at 50 (`jetson/camera.py`). A bigger
preview would starve control the way map downloads already did. Preview
caching by source, frame, and settings can skip a repeat decode, but it has
to keep the original timestamp. That caching belongs to the preview owner,
not this change.

At 10 req/s a 20 kB JPEG is 1.6 / 0.5 = 3.2x the bytes of `low` H.264
(500 kbps). That is why the viewer prefers H.264 when the rover offers it.

## Profiles

`low` is the default. `hq` is opt-in (`--video-profile hq` or
`FARSIGHT_VIDEO_PROFILE=hq`; `HQ` is accepted and lowercased, so the rover
service still starts). Explicit `--video-width`, `--video-height`,
`--video-fps`, and `--video-bitrate` still override the profile. Bounds are
unchanged: fps 1..30, bitrate 64000..4000000, even sizes 16..1920.

The encode box is filled by stretching the camera picture, the same resize
the encoder used before profiles. Detection boxes are fractions of the camera
picture, and the viewer draws them across the whole video frame, so black bars
would put every box in the wrong place. Stretching keeps a box on the person.

`hq` does not buy extra sharpness on the rig. The phone sends 640x480
(`CaptureController.swift`) and native capture sends 640 px JPEGs, so the
1280x720 box is that same picture stretched wider, into 16:9. What `hq` buys
is fewer compression artifacts (1.5 Mbps and `veryfast`/`main` instead of
500 kbps `ultrafast`/`baseline`) and 15 fps instead of 10. The native LiDAR
producer still caps fresh video at 6 fps, so the extra fps does not show up
on that path.

Known cost of `hq`, for the human deciding: the panel is not fixed 4:3. The
viewer resizes its canvas to the video's own size (`resizeCameraCanvas`,
`quest/src/rover.ts`), then rescales the panel to match that aspect
(`cameraScreen.scale` in `drawCamera`). With `hq` the panel becomes 16:9,
about 3.0 x 1.69 m, and shows the 4:3 phone picture stretched about 33%
wider (1280/960 = 4/3). Detection boxes still line up, since both the
picture and the boxes fill the frame at the same fraction. If the wide look
is worse than the artifacts it removes, a 960x720 `hq` box keeps 4:3 with no
stretch; it is a one-line change in `PROFILES` and inside the size bounds.

| | low (default) | hq (opt-in) |
|---|---|---|
| Encode box | 640x480, picture stretched to fill | 1280x720, picture stretched to fill |
| fps | 10 | 15 |
| Target | 500 kbps | 1.5 Mbps |
| x264 | ultrafast, baseline, level 3.0 | veryfast, main, level 3.1 |
| Latency | zerolatency, 200 ms VBV (`bitrate // 5`) | same |
| `/camera.jpg` | today's preview | today's preview, not raised |

`--video-encoder` (`FARSIGHT_VIDEO_ENCODER`): `libx264` is the default and
what runs unless a run asks for hardware. `nvenc` tries `h264_nvmpi`, then
`h264_nvv4l2enc`, and only if `ffmpeg -encoders` lists one. A listed chip
that fails to open falls back to `libx264` instead of crash-restarting. The
same fallback runs if the chip opened once and a later reopen never produces
an init. `/health` `video` does not name the encoder: it only has
`enabled`, `ready`, `error`, `fps`, and `bitrate`. The encoder list is only
probed when a run asked for a hardware encoder, and then once per process,
not on every restart. A plain `libx264` start does not probe. The Orin Nano
has no NVENC, so leave the encoder blank there. This Mac's ffmpeg lists
`libx264` and `h264_videotoolbox`, not either Jetson encoder, so the numbers
below are software.

## Laptop numbers

Server: `python -m jetson.server --demo --simulate-rover --detector none --h264`
on `127.0.0.1:8765`. Source banner would read SIMULATED. H.264 used the
viewer's two-request window for 8 seconds. Bitrate is payload bytes times 8,
over the measured wall clock.

The simulated camera is 160x120 and it moves: the pose yaws and translates
(`jetson/mapping/demo.py`), it is not a still. Both profiles stretch that
picture to fill the encode box, so `hq` spends bits on a blown-up 160x120,
not on extra detail. Measured this way, stretching, not the old black-bar
padding, `hq` ran at 1.433 Mbit/s. That is right at its 1.5 Mbps cap and
about half the ~3 Mbit/s link, before control, detections and health. An
earlier 0.751 Mbit/s number was the padded encoder and is not this code.

| Path | low | hq |
|---|---|---|
| `/ws/video` | 0.447 Mbit/s, 9.75 fps, `avc1.42C01E` | 1.433 Mbit/s, 14.62 fps, `avc1.4D401F` |
| Health | fps 10, bitrate 500000, ready, no error | fps 15, bitrate 1500000, ready, no error |
| Output size | 640x480 (160x120 picture, stretched to fill) | 1280x720 (160x120 picture, stretched to fill) |

`hq` stayed under its 1.5 Mbps cap here, but it already uses about half the
~3 Mbit/s link by itself. That does not make it the default. This is one
laptop, the source is 160x120, and control, detections and health were not
on the wire. A real 640x480 phone frame will not match this number. Do not
turn `hq` on for the DERP link until the real path is measured with those
still under 500 ms.

`/camera.jpg` was not re-measured. Its encode is unchanged, and this change
is the H.264 path. The earlier laptop poll (same server, 10 req/s, 8 s)
was 3.27 kB mean, 0.271 Mbit/s, 10.12 unique frames/s. A real 640 px JPEG
will be larger. Use the 20 kB / 1.6 Mbit/s row above for the link budget.

## Quest texture

The VR panel is a canvas texture, not an `XRQuadLayer`. The upload uses
bilinear filtering and no mipmaps (`LinearFilter`, `generateMipmaps = false`),
and the color space stays sRGB to match the renderer output. A second linear
decode would wash the picture out. The canvas is the decoded frame's own
size, so the draw does not scale into a smaller bitmap first. This is the
displayed picture only. It does not change poll timing, freshness, or the lease.

No before/after sharpness numbers. This Mac has no headset, and a desktop
browser was not measured. The settings are the usual ones for a video texture:
bilinear, no mip chain, sRGB once. They do not change bytes on the wire.

An `XRQuadLayer` would put the video on a compositor layer and skip the
canvas-to-texture upload, which is the expensive part of this path. It would
not shrink H.264 bytes on the wire, and the current view draws detection boxes
onto the same canvas, which a quad layer does not take. Adding it needs a
measured headset frame time before it replaces the judged view. Not in this
change.
