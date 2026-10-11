# Mesh LOD raster regression — 2026-10-10

All ten cases passed on native ANGLE Metal / Apple M1. This tests production renderer snapshots `2eab918` and `7a98b13` against the same connected, synthetic green plane. No real room, sensor, headset, cellular route or performance acceptance is established.

| Pinned detail tier | Before: triangles / interior coverage | After: triangles / interior coverage |
|---|---|---|
| 0–1 | 2,560 / 100% | 2,560 / 100% |
| 2–3 | 1,280 / 50% | 2,560 / 100% |
| 4 | 640 / 24.56% | 2,560 / 100% |

The candidate keeps the original index order at every tier. The fixture checks actual `gl.drawElements` submissions, draw range, captured renderer metadata and raw framebuffer pixels inside the plane's projected silhouette. Snapshot triangle counts alone do not establish submitted geometry. Existing layer hooks hide display grid and point clouds, and flat/unlit rendering isolates mesh coverage from lighting and other overlays. Allocated pixel dimensions still follow each real detail tier.

`evidence.json` records exact Git SHAs, renderer/dependency hashes, browser/backend, LOD parameters, projected sample region and coverage. The browser and private loopback server were explicitly closed after successful completion. Timing is not benchmarked, and this does not attribute every hole in a captured room to the repaired policy.

Prepare without opening Chromium:

```sh
node tools/tests/mac_render_mesh_lod.browser.mjs --prepare
```

On a Mac with project Chromium/Three dependencies, reserve a native GPU test slot before running:

```sh
FARSIGHT_MESH_LOD_OUTPUT=/tmp/farsight-mesh-lod-browser.json node tools/tests/mac_render_mesh_lod.browser.mjs --run --gpu-go
```

The test serves the pinned production files verbatim from Git without changing the checkout. It generates its own SIMULATED mesh and never opens camera, control or physical-device sockets. A software-renderer fallback fails the native-backend assertion.
