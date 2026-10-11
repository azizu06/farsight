# Direct canvas test

Run the existing Mac asset server without starting an encoder:

```sh
.venv/bin/python -m tools.mac_render --no-browser --port 8092
```

Open [the canvas](http://127.0.0.1:8092/render/canvas-lab.html). It fills the
browser viewport, keeps the grid visible, and has no text, toolbar, or feature
panels. Drag to orbit, scroll to zoom, and right-drag to pan. Dragging
stops the automatic orbit. The default fixture uses two million points; the
maximum is **20 million**. Set `?points=20000000` to exercise that maximum.

The room is **synthetic test geometry**, inspired by
[Pip Scout's point renderer](https://github.com/azizu06/pip-scout/blob/3f703d93b45142227baff75baa1c1413ed5ade7d/dashboard/src/PointCloudLayer.tsx)
and its persistent scan canvas. These points are surface samples, not unique
measured LiDAR cells. The test uses Farsight's existing Three.js renderer and
layer interface. Points stay in one reusable GPU geometry: Float32 positions
and normalized Uint8 linear RGB consume 15 bytes per point, or 300 MB for
20 million. Buffers update when the load changes; camera movement does not
rebuild or upload them. The canvas renders on demand when the camera is still.
No JPEG capture or video encoding runs in this path. The opt-in
`presentation=canvas` path does not preserve a capture backbuffer, and the
synthetic `fixture=1` path skips live map polling. MSAA is off for the point
load test; the circular splat shader provides the point shape.

Reproduce the desktop test and save real canvas screenshots and timing:

```sh
node tools/tests/canvas_lab.browser.mjs --points 20000000 \
  --out /tmp/farsight-canvas-20m
```

Aziz owns performance tuning and target measurements on the **M5 Pro with
48 GB RAM**. This chat's correctness checks ran on an **M1 with 8 GB RAM
under concurrent heavy AI load**, as reported by the user; their frame rates
describe that loaded test host.

The report records actual GPU provenance, point uploads and draw counts,
native canvas dimensions, requestAnimationFrame cadence, CPU draw submission
time, and image-export calls. Screenshots happen after timing. `--software`
explicitly selects SwiftShader. CPU submission time is not GPU completion
time; desktop frame cadence is not headset performance or sensor latency.
The console-only `window.__canvasLab.stats()` also exposes these measurements.

`?source=live&upstream=http://127.0.0.1:8092` uses that server's configured map
upstream. This reuses the existing live cloud's capacity and receiver; the
20-million fixture ceiling does not change live retention, the Jetson, or VR.
