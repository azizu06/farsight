# Actual renderer stereo acceptance — 2026-10-10

PASS on checkout `97d9063f80f17e43736280e7b3fc193b432592fa` including strict pair guards from `85c7253`. Synthetic SIMULATED markers only; no physical headset, real-room reconstruction, dense capacity or frame-rate acceptance claim. All fixture browser/GPU/encoder processes are closed; no rig or shared service was restarted.

Actual production page and pose/capture adapter rendered two distinct 640×720 eye halves in a 1280×720 JPEG. Near disparity was 27.982 pixels and far disparity 9.983; asymmetric marker centroids agree with Three.js projection math within 0.4 pixels. Exactly two main scene renders occurred, with viewport/scissor restoration. Current-session pair metadata, seven invalid/disconnected cases, reversed-pair rejection and recovery passed.

The actual `RenderEncoder.ffmpeg_command` selected **h264_videotoolbox** with explicit hardware=True. One captured pair encoded and decoded at 1280×720 with preserved parallax. This is a single-frame functional codec test, not latency/throughput qualification. The earlier private test passed a string to a bool API; that was fixture misuse, not a product selector bug.

`evidence.json` contains raw expectations, pixel measurements, metadata, viewport traces and the encoder command. `packed-pair.jpg` is the actual raster; `packed-pair.mp4` is its encoded counterpart.

## Rerun

Run from the repository root with Node/Chromium, Three.js, FFmpeg/ffprobe and Python dependencies installed. Coordinate an exclusive GPU test slot first. `--prepare` is CPU-only; GPU acceptance is not inferred from the skipped wrapper.

```sh
FARSIGHT_ROOT="$PWD" node docs/qualification/mac-render-stereo-20261010/mac_render_stereo.browser.mjs --prepare
FARSIGHT_ROOT="$PWD" FARSIGHT_PYTHON="$PWD/.venv/bin/python" FARSIGHT_STEREO_GPU_GO=1 python3 docs/qualification/mac-render-stereo-20261010/test_mac_render_stereo_browser.py
```

The test uses random loopback HTTP ports, SIMULATED markers and no control sockets or physical motion. It defaults to `/tmp/farsight-stereo-browser-evidence`; set `FARSIGHT_STEREO_OUTPUT` to preserve another run. It tests the current `FARSIGHT_ROOT` source, so pin the desired revision in a separate checkout for comparison.

## Tested source hashes

- `tools/mac_render/render.mjs`: `f38eaeb79e0354b1c853c30881389348a1cd26e1b72a435b6dc12e953b48f076`
- `tools/mac_render/stereo-pass.mjs`: `9abd6a944ff1c6c8801d393c876c76a8727a8dd36ffcf38f103e03c47635a11d`
- `tools/mac_render/server.py`: `54a3380669ebbc382a1325f1cda10d36ac260975c64306fd50712ff2460898ec`
- `tools/mac_render/protocol.py`: `4d62d723cb4f6b17bcb6a8988a37a6eb58343378a7ff3d46ad5f6ff32aac3240`
- `tools/mac_render/stereo.py`: `121c3173f8fcbdc799f57338f6344f28bb1330fdfdece7fa5bdcd7e238bec157`
