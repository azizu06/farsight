# Continuous Mac SBS stream: software evidence, 2026-10-10

Actual production MacAutoView/H264Viewer, native WebSockets and MSE were exercised in the canonical same-page app at `http://127.0.0.1:18873/?macview=1&stereo=1&diag=1`. The relay owner reported client build `97d9063` and renderer fixture `1af4771` (including strict paired-eye guards). These are owner-reported served revisions, not independently hashed bundle provenance. Renderer 18870, SIM upstream 18871 and relay 18873 remained owner managed. This fixture did not restart them.

Functional stereo evidence passed: coded 1280×720, declared `sbs-left-right` with 640×720 per eye, painted MAC RENDER · STEREO label, 205 echoed pairs matched sent session/frame identities with ordered left/right labels, and 210 distinct advancing video media times. The observed eight-second window was 8.0165 seconds. Decoded video pixels were all nonblack at 128×72; mean corresponding-half RGB difference was 6.207. This comparison establishes distinct halves in this scene; the separate geometric stereo fixture establishes expected parallax. Echo matching here does not independently compare every matrix element. There were zero seeking/seeked events and no browser errors or foreign socket attempts.

## Conditions and limitations

- Source was explicitly SIMULATED; XR and Quest2 controllers were IWER emulation. The harness overrode simulated gamepad `connected`, entered emulated VR, and tapped A through the actual UI. This can arm autonomous behavior only on the agreed SIM relay. It is not a physical headset, measured scene, dense scan capacity, motor safety or pose-to-photon acceptance test.
- This consumer (process handle 88486) overlapped the priority manual XR functional test (handle 94410). Both have closed. Native Metal was requested. GPU identity was not independently probed by this harness. **The observed 26.196 unique media frames/s and latency quantiles are under concurrent GPU load, not an isolated performance baseline.** Callback gap p95 was 66.7 ms, maximum 116.7 ms. Raw browser counters reported total delta 206 and dropped delta 58; these are retained verbatim and do not establish missed headset frames.
- Producer-side `age_ms` p50/p95 was 257.24/416.57 ms; `encode_pipeline_ms` was 256.81/415.72 ms, `capture_eval_ms` 27.04/48.60 ms, `server_wait_ms` 33.77/63.67 ms and `pose_to_frame_ms` 7.16/17.09 ms. These are encoded-fragment sidecar timings, not camera age or decoded-frame/presentation age. No independent end-to-end decoded freshness measurement was added.
- `controlSockets = 2` records **cumulative constructor calls**, including potential reconnects. There were no open/close/live counts, so simultaneous single-control ownership is **UNVERIFIED**. The raw overall `passed: false` and `singleSimControlSocket: false` are preserved. This auxiliary check cannot diagnose multiple simultaneous controllers from cumulative creations. No causal reconnect claim is made.
- The last outgoing pose retained the existing `stereo: false` field while carrying two ordered XR eyes. Actual producer output declared stereo and echoed the pair. This report records the observed contract without changing the product.
- Pixel readback occurred once after the cadence sample and took 43.6 ms; it can affect subsequent snapshots. The PNG captures the actual same-page UI, while direct video-only pixel statistics exclude HUD pixels.

## Rerun

Use an agreed owner-controlled **SIM-only** relay on 18873 configured with the production SBS renderer on 18870 and matching built Quest assets. Obtain the exclusive GPU slot before running; do not point this harness at physical control services. Install/cache Playwright Chromium and IWER from the repository Quest dependencies. The preserved harness uses the original absolute repository/dependency path and writes to `/tmp/farsight-iwer-continuous-sbs-evidence`. On this machine:

```sh
node docs/qualification/mac-render-sbs-stream-20261010/continuous-sbs.mjs
```

For another checkout, update the two dependency paths, recorded served revisions, SIM origin and output directory in a private copy. Before using it as an ownership acceptance gate, track socket open/close/live counts separately from cumulative creations. Browser shutdown is in `finally`; the run adds no diagnostic reseek suppression and does not fabricate stereo eye transforms. Do not interpret its legacy overall false as failure of all stereo functional checks.

## Artifact SHA-256

- `evidence.json`: `19649343bd0cc8c073d44c9ec79bc8fce8ee5afb66de014a7b7d65b052b5f764`
- `screenshot.png`: `f3f756259340cd583cb6e204fc3759110ad7b3f98b6686f3ae80e7c0e4aa2091`
- `continuous-sbs.mjs`: `8bf75a9168bc9f76532ad5d5b41defa59122d13f1f14a05707594a6fc7e5662d`
