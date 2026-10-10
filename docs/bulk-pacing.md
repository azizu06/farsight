# Optional Python bulk pacing

The Python mapping server accepts `--bulk-limit-mbps N` and
`create_app(bulk_limit_mbps=N)`. The default is **0 (disabled)**. One shared
`app.state.bulk` budget covers point/mesh/PLY HTTP bodies, mission snapshots,
TTS bytes and legacy map WebSocket payloads. Live video (camera JPEG, MJPEG,
depth JPEG and atomic H.264 `/ws/video` payloads) uses a separate
`app.state.live` pacer, `--video-limit-mbps` / `FARSIGHT_VIDEO_LIMIT_MBPS`,
default **0 (unpaced)**: sharing one budget made each 40 KB camera JPEG wait
behind map snapshots and pushed headset video age past the 500 ms motion stop.
Health, control/permits, detection/status JSON and incoming phone frames use
neither budget. Static Quest assets are outside both limiters.
Python ownership is coordinated on board #24; Rust remains Devin's area.

An opt-in example for a previously measured link:

```sh
python -m jetson.mapping.server --bulk-limit-mbps 1.4
```

This number is illustrative, not a qualified demo profile. Enabling a cap does
not qualify the physical link or reserve network capacity for control.

One `BulkPacer(rate_mbps)` instance must be shared across all selected bulk
egress on a server. Default rate zero disables pacing, retaining each incoming
HTTP block and WebSocket packet without clocks, locks, or sleeps. Rates are
finite, nonnegative decimal Mbps; framing/TLS/IP overhead is not counted.

HTTP snapshot/JPEG/MJPEG/TTS bodies are **streamed** through `chunks()`:

```python
from contextlib import aclosing
from jetson.bulk import BulkPacer

bulk = BulkPacer(1.4)

async def body(packet):
    async with aclosing(bulk.chunks(packet)) as chunks:
        async for part in chunks:
            yield part
# Use body(packet) as the StreamingResponse body, with the existing media type.
```

`chunks()` also accepts sync/async iterables of byte blocks. Each enabled-mode
HTTP chunk is at most 16 KiB and at most 25 ms of byte credit, with a minimum
one-byte chunk. At rates below 40 bytes/second, one byte itself exceeds the
25 ms physical serialization allowance; byte granularity cannot avoid that.
Do not collect the iterator back into a single body after waiting: that would
recreate an unbounded post-wait burst.

Each chunk holds one FIFO pacing slot through its consumer's handoff. Close the
iterator when the response ends or is abandoned; the example uses `aclosing`.
The integrated `PacedStreamingResponse` explicitly watches HTTP disconnects,
cancels a pending send/wait, and closes both pacing and source iterators before
returning. This also releases a slot when the ASGI version would otherwise
depend on a stalled send to discover disconnection. Existing content lengths,
compression and source-age headers remain intact.
This observes downstream backpressure and rotates queued stream requests between
chunks. A slow consumer can delay other bulk consumers; existing transport send
deadlines must remain in place. Control and permits use **none** of these slots.

For an existing atomic H.264/WebSocket payload:

```python
await bulk.send_packet(fragment_bytes, websocket.send_bytes)
```

This calls the sender exactly once with the original bytes. Enabled pacing
rejects packets exceeding `max_atomic_bytes` (default 256 KiB); it never invents
framing or splits a WebSocket message. Legacy multi-megabyte map messages should
use the existing status/HTTP snapshot path. If preserving a legacy large atomic
message is mandatory, its owner must explicitly select and qualify a larger
bound instead of assuming it has the same burst behavior as HTTP chunks.

**Atomic limitation:** waiting for an entire message accounts for its aggregate
byte cost but cannot prevent that one message from entering the transport as a
burst. A packet larger than the 25 ms credit budget does not obey that short
output-window bound. Small HTTP chunks provide bounded handoff bursts; this
API cannot control socket/TLS/kernel buffering or guarantee physical wire QoS.
Choose a bulk rate below measured total-hop capacity, leaving room for control,
protocol overhead, and variability, then qualify the real loaded link.

Accounting uses a monotonic virtual finish time. A completed sleep pays the
current request once; that paid sleep is never credited again on the next call.
An interrupted pacing wait reserves no future debt. Cancellation while queued,
while waiting, or during sending releases its slot; a send cancelled after
admission remains charged because partial transport delivery is unknown.

Integration leaves `/ws/control`, control status/permits, watchdogs,
freshness clocks and wire fields untouched. H.264 `age_ms` is calculated at the
actual handoff after pacing; `server_wait_ms` retains its segment-readiness
meaning and excludes admission wait. No capture timestamp is refreshed.
Oversized atomic video payloads close with code 1009 and a bitrate/resolution
hint; oversized legacy map payloads close with code 1009 and direct the client
to `/ws/map?points=http` plus streamed HTTP snapshots. Video initialization
monitors disconnects during admission and releases its FIFO slot on abort.
Source-side pacing must
precede the constrained Jetson→operator hop; relay-only limiting cannot undo
bytes already sent across it.

Controlled-clock tests observe aggregate bytes/time and competing stream
progress, not just configured sleep constants:

```sh
python -m unittest jetson.tests.test_bulk jetson.tests.test_bulk_integration -v
```

These tests are application-level policy evidence. They do not qualify a real
cellular path, socket behavior, Quest display age, or motor stop timing.
