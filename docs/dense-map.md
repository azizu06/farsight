# Dense measured map

The existing map is a coarse preview: 4 cm fusion, 120k retained voxels and
30k served points by default. The additive dense lane samples confident
native depth pixels and their RGB at their ARKit pose, one real sample per 1 cm
cell. It never interpolates, duplicates samples to increase counts, or
changes navigation/control geometry. Invalid depth, limited tracking,
duplicate captures, pause and session/epoch resets retain their existing rules.
Dense capture remains active with `--fusion-bypass`; that flag still bypasses
coarse voxel/mesh fusion only.

Dense integration runs on a single background worker, with at most one pending
frame plus one active frame. New frames replace the pending frame; capture and
control never wait for dense reconstruction/integration. The default budget is
**8 jobs/s and 12,288 measured pixels/job**; sampling rotates across valid native
pixels over accepted jobs. Vectorized record construction retains exactly the
previous measured bytes and ordering. A thread-CPU budget lowers admission when
projection/integration becomes costly; it does not accumulate old frames or add
an extra recovery delay after a visual lock wait. The Jetson working cache has
a 30-second lifetime; the Mac retains already observed room detail. Delta reads copy one
bounded journal page, rather than rebuilding all pending changes; the Mac
polls at 10 Hz when caught up and drains pending bounded pages faster.

Set **`FARSIGHT_DENSE=0`** in the Jetson service environment and have the service
owner restart it to disable the visual worker completely. It is enabled by
default; no CLI flag is required. `FARSIGHT_FUSION_BYPASS` and legacy visual caps
are independent. `/health` includes `dense_worker` with enabled state, budgets,
processed/dropped/queued jobs, worker duration and error. `processing_ms` remains
the camera/coarse-frame path timing, separate from background dense duration.

Startup settings are `FARSIGHT_DENSE_HZ` (1–30, default 8),
`FARSIGHT_DENSE_FRAME_POINTS` (128–65,536, default 12,288), and
`FARSIGHT_DENSE_CPU_BUDGET` (0.05–0.75, default 0.15). Invalid values use the
bounded defaults. For the previous low-bandwidth profile, select 2 Hz / 4096.
`dense_worker.admission_hz` reports the current CPU-limited ceiling, `cpu_ms`
reports thread execution time, and `sampled_points` counts observations rather
than retained unique cells. The rig owner selects and records the deployed
budget using actual CPU/link measurements; source defaults do not establish
that a running service has loaded them.

## Launch and ownership

The rig/service owner must install this revision and restart the Jetson service;
the implementation worker must not restart rig services. No new Jetson flag is
required. Measured dense capture requires the existing `/phone` RGB-D lane;
RGB-only `/phone/rgb` and coarse ARKit mesh vertices cannot create missing
LiDAR detail. The phone must send real synchronized depth/confidence/RGB with
normal tracking.

The Mac relay owner starts:

```sh
python -m tools.demo --upstream https://JETSON:8443 --dense-map --build --quest
# Also consume Devin's world-baked native ARKit mesh chunks (neutral gray):
python -m tools.demo --upstream https://JETSON:8443 --dense-map --build --quest \
  --geometry-upstream http://127.0.0.1:8484
```

Use the existing trusted `--ca-file` if needed. `tools.dense_map.mount()` can
instead mount into an existing relay before startup; `operator_relay.py` is
unchanged. Dense routes run behind its localhost/origin guard on the existing
8080 Quest USB tunnel. The new module has its own two-connection HTTP pool;
it does not consume control/video connections. Rebuild/redeploy Quest assets
through the owning deployment agent. `?view=map&walk=1` selects the existing
walk-through mode.

## Retention and display

Jetson keeps a 600k-cell working cache; budget eviction is not a deletion of
the Mac's retained detail. Mac retains up to five million unique 1 cm cells.
Real reset/removal clears the appropriate geometry; capture-session/epoch
changes always clear old coordinates. A bounded revision journal falls back
to an anchored, paginated snapshot if the client misses its history.
Snapshots never repeatedly reset per page, and replay changes since their
start to capture removals/updates during bootstrap.

The Jetson's RGB-D cache expires after **30 seconds**. The Mac keeps measured
RGB-D history for the capture session, up to its cell capacity. Producer cache
expiry tombstones and same-epoch snapshot bootstraps do not erase that history;
a true producer epoch change clears it. Snapshot pagination still expires after
30 seconds. Historical points can include earlier positions of moving objects;
they are observed visual history, not current navigation geometry. Optional
`retained_history:true` metadata identifies this behavior. Legacy clients receive
`ttl_s:86400`; clients that consume the history flag retain the room across a
source disconnect and report source age separately. Native ARKit anchors are a separate gray source
with explicit anchor replacements/removals and a 24-hour cache bound. Gray
mesh vertices are not labeled calibrated RGB or a million-point LiDAR scan.
The viewer prefers RGB-D when available and otherwise uses native geometry;
it never combines their coordinate origins.

Mac indexes 2 m spatial chunks. Viewer distance selects sample LOD; crowded
chunks increase sample spacing until at most 8192 **existing measured**
points remain. Up to 32 nearest chunks (262,144 uploaded points) fit the Quest
budget. Mac retains the full cloud independently. Unchanged chunk revisions
are not fetched again. Changed chunks replace only their own bounded GPU
buffer ranges, rather than a complete map download/upload. Round points are
12 mm in the world, with a projection-aware 1.25-pixel minimum. Live video
remains the reliable view of people. These budgets are software bounds, not
proof of 72 fps on the headset.

## Renderer API and actual measurements

`GET /dense/manifest?x=0&y=1&z=0` returns the selected epoch, source, retained
unique count and chunk IDs/revisions/LODs. `GET /dense/chunk.bin?id=X_Y_Z&lod=0`
returns one bounded replacement, with the epoch/revision in its binary header.
Use `source=geometry` explicitly for the gray native source, including chunk
requests. The default source is RGB-D when nonempty.

Dechante's Mac GPU renderer can read **all retained detail** from
`GET /dense/delta.bin?epoch=...&after=...&snapshot=true&anchor=...`.
Use `source=geometry` for the separate native cloud. The wire and cursor
contract is documented in `docs/interfaces.md`. It supports a large Mac
renderer without asking Quest to upload millions of vertices.

```sh
curl http://127.0.0.1:8080/dense/status
python -m tools.benchmark_dense --points 2000000
```

Status reports actual `jetson_points`, `mac_points`, compressed response
`tailnet_bytes` and average `tailnet_bytes_per_s` since this accumulator
started. These count response payload bytes, excluding HTTP/TLS overhead;
the hostname/route decides whether the measurement is actually tailnet.
Quest reports `uploaded_points`, drawn `points` from the prior rendered frame,
render-loop `fps`, `xr` and a report timestamp. `xr:false` is desktop evidence;
stale timestamps and missing values are not a headset result. Native assembler
`retained` counters and Mac gray unique counts are reported separately.
Historical observations are not retained unique points.

The benchmark uses a labeled synthetic unique grid and frozen expiry clock to
test capacity and binary compression, not capture quality or network/hardware.
Capture a before/after of the same real room, actual per-hop counts and
`xr:true` frame rate after deployment. Neither simulated test images nor
configured maxima complete that physical qualification.

Checks:

```sh
python -m unittest jetson.mapping.tests.test_dense tools.tests.test_dense_map
npm --prefix quest test
npm --prefix quest run build
npm --prefix quest run test:judge
node quest/tests/dense-map.browser.mjs
```

The additional dense browser
fixture checks bounded upload, incremental refresh and reset at desktop/mobile
sizes using Chromium; it does not emulate physical LiDAR or Quest GPU speed.
