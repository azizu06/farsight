# compiler_viewer

Hyper-realistic three.js viewer for `farsight-compile` output directories, with a
built-in static server inside the `farsight-compile` binary.

## Run

```sh
cd rust && cargo build --release -p farsight-compiler
./target/release/farsight-compile view <out-dir> [--host 127.0.0.1] [--port 8765] [--open]
```

`<out-dir>` is a compiler output directory containing any of:
`scene.glb`, `mesh.ply`, `points.ply`, `splat.ply`, `report.json`.
The viewer adapts to whichever artefacts exist (e.g. heatmap needs `support`
vertex data; textured mode needs a textured `scene.glb` or falls back to
vertex colour).

Then open the printed URL (or pass `--open`).

## Render modes

| Key | Mode       | What it shows |
|-----|------------|---------------|
| 1   | textured   | GLB mesh w/ textures (falls back to vertex colour PBR) |
| 2   | vcolor     | raw vertex colours, unlit |
| 3   | heatmap    | measured→interpolated: teal = measured, red = farthest from a measured sample (uses `support`/`_SUPPORT` attribute, metres) |
| 4   | normals    | world-space normal colours |
| 5   | wire       | wireframe over shaded mesh |
| 6   | points     | raw `points.ply`; orange points = `FLAG_INTERPOLATED` |
| 7   | splat      | Gaussian splats from `splat.ply` (INRIA 3DGS layout) |
| 8   | blend      | mesh + splats composited |

## Cameras

- `orbit` (default): drag to orbit, scroll to zoom, right-drag to pan.
- `human 1.6m` / `rover 0.3m`: WASD walk + mouse-look (drag). `Space`/`Ctrl`
  up/down, `Shift` boost. Rover preset moves 0.4× speed at rover eye height.
- `R` reset view, `F` frame the scene.

## Quality presets

`Q` cycles quality (high / medium / low):
- **high**: GTAO + bloom + FXAA, shadows 2048, full pixel ratio
- **medium**: bloom + FXAA, shadows 1024, pixel ratio ≤ 1.5
- **low**: FXAA only, no shadows, pixel ratio ≤ 1

An adaptive pixel-ratio governor samples frame time and steps resolution
down/up to hold ~60 fps on a MacBook.

URL params: `?mode=splat&q=medium&splatcap=40000` (splatcap subsamples
splats — used by the headless check on software rasterizers).

## Architecture

- `rust/farsight-compiler/src/view.rs` — tiny_http server. Serves the viewer
  bundle embedded via `include_bytes!` under `/viewer/*`, the output directory
  under `/out/*` (path-traversal safe), and `/api/manifest` listing artefacts.
- `index.html` — HUD shell + import map pinning `three` to the vendored build.
- `viewer.js` — scene setup (ACES tone mapping, sRGB, PMREM `RoomEnvironment`
  IBL, PCFSoft shadows), loaders, mode/quality/camera systems, HUD, FPS meter.
- `ply.js` — self-contained binary/ASCII PLY parser (scalar + list props,
  vertex + face elements, name normalisation incl. `diffuse_*`/`f_dc_*`).
- `splats.js` — compact WebGL2 splat renderer: instanced quads, covariance
  built in the vertex shader from texel-fetched scale+quat, opacity sigmoid,
  premultiplied-alpha accumulation, circular kernel + near/far clipping.
- `splatsort.js` — Web Worker doing a counting sort of splat keys by view
  depth (far→near) at most every 40 ms.
- `vendor/three/` — vendored three.js r180 + addons (no CDN needed at demo).

## Testing

```sh
python3 tools/compiler_viewer/test/make_fixtures.py /tmp/viewbench
node   tools/compiler_viewer/test/render_check.mjs --out /tmp/viewbench --shots /tmp/shots
```

`render_check.mjs` spawns `farsight-compile view`, drives headless Chromium
(SwiftShader), sweeps every mode + camera preset + quality level, captures
screenshots, and fails on any console error. Results land in
`results.json` beside the shots. `--emit-glb` additionally exercises the
GLB path by exporting `scene.glb` via GLTFExporter first.
