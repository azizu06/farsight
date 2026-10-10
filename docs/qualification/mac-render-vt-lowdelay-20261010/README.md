# Isolated synthetic Mac JPEG encoder diagnosis

Scope: three 90-frame synthetic rendered-room JPEG pipes at1280×720/30fps,
median61170B/JPEG. No existing renderer consumers, network endpoints, poses,
phone measurements or repository edits. Parent explicitly gave GO after the
acceptance consumer closed. Timings include FFmpeg API queue/delivery, not pure
hardware execution time or a promise of the actual app's latency.

| Case | Steady input→complete matching fragment p50 | p95 | First fragment |
|---|---:|---:|---:|
| Current FFmpeg VT arguments |401.66ms|402.97ms|406.36ms|
| Only add output-side `-flags +low_delay` |201.91ms|203.43ms|589.14ms|
| Existing libx264 fallback as causal control |70.44ms|85.39ms|71.74ms|

All270 decoded images carried the correct bit-marker ordinal0..89 for each
case. Every MP4tfdt/trun ordinal matched that picture, every fragment had exactly
one sample, and there were no dropped/duplicated frames. Median stdin drain
was0.05–0.07ms. First run repeated the VT A/B:401.69→201.97ms; its low-delay
startup was972ms, so startup improvement is not established.

Per-frame FFmpeg `debug_ts` lines were timestamped upon stderr receipt (both
encoder input and output directions), while complete MP4 fragments were
recorded on stdout receipt. Steady ordinal30..74 medians:

| Stage | Baseline VT | Low-delay VT | libx264 control |
|---|---:|---:|---:|
| Input→demux |33.47ms|33.46ms|33.49ms|
| Demux→decoded picture |1.32ms|1.25ms|1.27ms|
| Decoded picture→encoder input |0.06ms|0.05ms|0.06ms|
| Encoder input→encoded packet |333.43ms|133.48ms|2.05ms|
| Packet→mux input |0.02ms|0.02ms|0.004ms|
| Mux input→complete fragment |33.33ms|33.39ms|33.25ms|

The dominant measured delay belongs to the VideoToolbox encoder path's
asynchronous buffering/delivery, not stdin backpressure or slow JPEG decoding.
The remaining33ms input parser wait and33ms fragment boundary wait are separate.
These are receipt-stage measurements, not instrumentation of Apple's callback
or hardware compute duration. The production-compatible `allow_sw1` remained
unchanged; this diagnostic did not force hardware-only selection.

One narrow recommendation for the encoding owner: retain the decoder's existing
input `-flags low_delay`, and also set `-flags +low_delay` in the **output**
argument group after `-i` and before the output file. In FFmpeg8.1.2's native
VideoToolbox encoder, `AV_CODEC_FLAG_LOW_DELAY` enables
`EnableLowLatencyRateControl` and its one-in/one-out mode. `-realtime1` alone
sets a different property. No bitrate, GOP, profile, rate, fragments or guards
were changed in this A/B.

Primary source:
https://github.com/FFmpeg/FFmpeg/blob/n8.1.2/libavcodec/videotoolboxenc.c#L1711

Reproduction (only while the acceptance encoder owner permits shared resources):

```
.venv/bin/python docs/qualification/mac-render-vt-lowdelay-20261010/reproduce.py --cases baseline output-low-delay x264-control --output /tmp/farsight-vt-latency-verified.json
python3 docs/qualification/mac-render-vt-lowdelay-20261010/stages.py /tmp/farsight-vt-latency-verified.json
```

Exact command arrays, frame input/fragment times and timestampeddebug lines are
in the checked-in `evidence.json`; the command above writes a fresh report under `/tmp`. Individual MP4 and stderr artifacts
are `/tmp/farsight-vt-{baseline,output-low-delay,x264-control}.{mp4,stderr}`.
The generated room image is checked in as `synthetic-room.png`; if absent, the
script generates a deterministic gradient fixture and reports a different JPEG
size. Fresh run artifacts stay under `/tmp`; no product change was made. The reproducer replays recorded commands, so later product encoder changes do not silently change its baseline.
