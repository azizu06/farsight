# Published 38b10 continuous SBS: software verification

The one consumer, exec handle 42190, closed with exit 0 after an 8.028-second sample. Actual canonical MacAutoView/H264Viewer, native browser WebSockets/MSE and IWER Quest2 emulation were used at `http://127.0.0.1:18873/?macview=1&stereo=1&diag=1`. The fixture owner reported producer/client SHA `38b10eff625b1b81c3b54431df76b77a7ba72bb4`: renderer PID10456 on18870, SIM PID10458 on18871, operator PID10517 on18873. Owner-managed services were not restarted by this harness.

**Functional PASS:** 1280×720 packed `sbs-left-right` output (640×720 per eye); 209 matching sent session/frame identities with ordered left/right echo labels; 183 unique advancing media times; visible nonblack/distinct halves; AUTONOMOUS chip and MAC RENDER · STEREO label; one simultaneously OPEN control connection; no foreign socket attempts; no geometry HTTP transfer overlapping the sample; served JavaScript bodies hashed. Raw overall true and every check are preserved in evidence.json.

**Fresh playback is not proven uninterrupted:** the sample painted MAC VIEW LOST · LOCAL MAP twice, recorded two seeks/two seeked events, and the maximum callback gap was 916.5 ms. Overall functional PASS is not an acceptance claim for smoothness, freshness or hardware usability.

## Conditions and scopes

- Concurrent desktop SwiftShader judge and visible disconnected canvas were reported by the parent during this run. There was no other native performance consumer. **22.795 unique media frames/s and all latency numbers are under these conditions, not an isolated performance baseline.** Native Metal was requested; this harness does not independently query the GPU renderer. Callback gap p95 was66.6ms. Browser raw quality counters delta total185/dropped31 are preserved and do not establish missed XR frames.
- Source is explicitly SIMULATED. Quest2 XR/controllers are IWER software emulation. The harness overrides only simulated gamepad connected, enters emulated VR and makes an A tap through the current actual UI; no trigger input, diagnostic reseek suppression, fabricated eye transforms, physical hardware or real rig commands were used. The agreed relay routes control only to the SIM fixture. Matching echoes test session/frame identities and eye labels, not independent equality of every matrix element.
- `age_ms` is producer-side encoded-fragment age: p50/p95238.12/495.57ms; encode_pipeline235.34/478.35ms; capture_eval16.61/51.54ms; server_wait33.01/72.29ms; pose_to_frame7.63/21.81ms. These are not camera freshness or end-to-end decoded/presentation age. Nested map metadata age is also preserved separately in the raw sidecar.
- Control created two sockets sequentially: first OPEN2036.9ms, CLOSE2604ms(code1000); second OPEN3624.4ms. There were two opens, one close, zero errors, `maxOpen=1`, and `live=1` at end of sample. No control events occurred during the sample6795.5–14824ms. This is observed browser OPEN ownership through the sample, not physical safety acceptance.
- Local geometry HTTP observation recorded an initial mesh200 and dense manifest404 before the sample and another dense manifest404 after the sample. **Zero points/mesh/dense downloads overlapped the sample.** This is only a network boundary check; it does not measure CPU/GPU work avoided, and it does not claim downloads never happen during recovery.
- The direct video-only read was one post-sample128×72 observation:9216/9216nonblack, mean corresponding-half RGB difference5.470. Readback took183.6ms and can stall later snapshots. PNG is the actual canonical same-page UI. Distinct halves here are combined with the separate geometric parallax fixture; they are not a depth calibration by themselves.

## Actual served script provenance

Only responses already requested by the browser were read; no new fetch was sent to obtain hashes. Both response bodies matched files in the owner-reported clean build `/tmp/farsight-minimal-published.SynxEu/quest/dist` in a subsequent CPU-only comparison:

| Served URL | Bytes | SHA-256 |
| --- | ---: | --- |
| /assets/main-BhVT_Ny4.js |171620|8a5c6e3271e2604a380a2e3ef5d7ce3f0576eaa4cce56fa9122ac6b9ff57afc3|
| /assets/VRButton-Bz7hFPfn.js |510627|308ad42d8c7777b7b8f23f75ef6847977bc2f4a44c368b54d7523b64f03d5d98|

The loaded producer/client revisions are owner-reported. Response hashes verify those exact served bytes and their match to the stated clean-build directory; this harness did not independently reconstruct the Git-to-bundle build.

## Rerun

Obtain a **new explicit owner GPU GO** and stable owner-held SBS renderer/SIM/relay first. Never use a physical control origin. The retained harness has original absolute Playwright/IWER dependency paths and fixed SIM relay18873; adapt only in a private copy for another environment. Do not reuse these old process handles as authorization.

```sh
node docs/qualification/mac-render-sbs-38b10-20261010/cpu-test.mjs
FARSIGHT_SBS_GO=1 \
FARSIGHT_CLIENT_SHA=38b10eff625b1b81c3b54431df76b77a7ba72bb4 \
FARSIGHT_PRODUCER_SHA=38b10eff625b1b81c3b54431df76b77a7ba72bb4 \
node docs/qualification/mac-render-sbs-38b10-20261010/continuous-sbs.mjs
```

Output defaults to `/tmp/farsight-continuous-sbs-current-evidence`; set `FARSIGHT_SBS_OUT` for a fresh evidence directory. Browser shutdown is in `finally`. CPU tests cover sequential reconnect versus simultaneous OPEN sockets, failed connections, same-origin/path refusal before native constructor, geometry-download classifier and absent new-GO refusal. No shared product files changed.

## Artifact SHA-256

- `continuous-sbs.mjs`: `7aacffd3e6fbaf3820b48088371edc6090adfe774414f21cab35f5713dedd318`
- `cpu-test.mjs`: `5ab1dca4c5d05f0522a136c0201f9cdc5ebff814783c7863a11f8dc1dbe6474b`
- `evidence.json`: `0807715aeb04ee38ca86800353fa27f5b8dd79b541ee42f99b83d0651a31d4f2`
- `screenshot.png`: `64edb48854271470baf3c4f6c825acbcaaed0ea79543abf6e61a73308678ae0c`
