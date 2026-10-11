# Explore the reconstructed room

Open **`/explore.html`** on the operator relay in the Quest browser and select
**Enter VR**. This is a standalone viewer: left stick walks and strafes, right
stick snaps 30° left/right or rises/descends, **A** opens help, **B** returns to
the starting position. Physical head movement stays tracked at room scale.
The scan fills the view; controls appear only when requested or the stream is unavailable.

The Mac does dense fusion, surface meshing, lighting, per-eye rendering and H.264
encoding. Quest decodes video and draws two eye quads. It downloads no point
cloud, builds no mesh and opens no rover control connection. Denser retained
points and future renderer layers use the same visual stream.

Start the Mac stereo renderer against the existing map relay:

```sh
python -m tools.mac_render --upstream http://127.0.0.1:8080 \
  --port 8091 --stereo sbs-left-right
```

The operator relay needs its existing `--render-upstream http://127.0.0.1:8091`
configuration. Build `quest/` with `npm --prefix quest run build` and serve the
result through that relay. Existing Quest USB/HTTPS setup applies; see
[transport setup](transport-setup.md). The launch commands are setup examples,
not a claim that the physical rig was redeployed.

On the Mac, drag to orbit, right-drag to pan, scroll to zoom, or use **WASD** to
move and **Q/E** to rise/descend. **Home** frames the scene. Help is available
with **?** or **Escape**. For direct local development, open
`http://localhost:5173/explore.html?mac=http://127.0.0.1:8091`; on Quest use
the same-origin relay so localhost refers to the correct machine.
Only one viewer can own the renderer's visual camera at a time; close an
inspecting desktop tab before using a different headset tab.

The Mac samples at most 4,096 retained positions once a second to send a small
display anchor alongside existing map metadata. The initial view frames that
scan, and its low height percentile aligns the virtual floor. It never creates
floor geometry. The floor is held for that map identity, so growing captures
do not lift the viewer; `?floor=-1.2` overrides its height in map metres when needed.
Older renderers without this metadata still stream; their initial floor is zero.

Synthetic/replay sources remain labeled. Empty areas stay empty. A lost stream
shows the orientation grid and a waiting message using the existing one-second
picture expiry. The world remains a remotely rendered stereo image: rotation
uses the echoed eye pose, and translation needs a new Mac frame. No per-pixel
depth reprojection is claimed.

Software qualification: `node --test quest/tests/explore-navigation.test.mjs
tools/tests/scene_anchor.test.mjs`, `node quest/tests/explore.browser.mjs`,
`npm --prefix quest run build`, and the existing judge flow. Browser fixtures
and local simulated rendering do not establish physical headset comfort,
Wi-Fi/USB latency, stereo quality or real scan completeness.
