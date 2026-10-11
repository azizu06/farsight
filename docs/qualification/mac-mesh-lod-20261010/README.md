# Mac mesh LOD CPU qualification — 2026-10-10

The witness runs exact production `uploadMesh`, `scatterOrder`, `meshDrawCount` and pooled attribute/index helpers against a synthetic connected 8×8 grid using real Three.js `BufferGeometry`. It does not launch a renderer, GPU, stream consumer or browser.

`baseline.json` is the original unchanged result from the pre-fix audit. `source-before.mjs` preserves that exact clean source (`85c7253`). `source-fixed.mjs` is the exact `git show` source at owner fix `7a98b132473c8ac3b967afb5423cbc22282bbfa1`. `source-manifest.json` pins both source hashes and commits. The reproducer rejects altered sources and mismatched expectations.

From a checkout with Quest dependencies installed:

```sh
node docs/qualification/mac-mesh-lod-20261010/repro.mjs --expect-fixed --out /tmp/farsight-mesh-lod-fixed-7a98b13/fixed.json
node docs/qualification/mac-mesh-lod-20261010/repro.mjs --source source-before.mjs --out /tmp/farsight-mesh-lod-baseline-replayed/baseline.json
```

Use `--repo /absolute/checkout` to locate another checkout's installed `quest/node_modules/three`. The output directory also receives exact source/extracted code and grid input. Run outside the archived directory to preserve the saved evidence.

The fix must retain 128 triangles, 64 m², one edge-connected surface component and exact original index order at fractions 1, 0.5 and 0.25. This is software topology evidence on a synthetic input. It does not establish the simulator PNG's causal LOD, headset rendering/performance or real-room geometry quality.
