# Synthetic replay plumbing fixture

Seven JPEGs and timestamped detector/control telemetry captured by the canonical
Python server with `--demo --simulate-rover --record --mission-dir` on Oct 10.
This is ray-cast SIMULATED data with empty detector boxes and static 120 cm
virtual range, not physical rover evidence. Duration is about 1.36 seconds.

```sh
python -m jetson.server --replay tools/fixtures/replay-demo --h264
```

Verified on a local CLI server: `/health` replay/source flags, JPEG retrieval,
recorded range, inert WebSocket control, detection loop progression, H.264 init
and video segment. See `docs/demo-checklist.md` for real recording instructions.
No physical Quest, camera, rover or Rust replay validation is claimed.
