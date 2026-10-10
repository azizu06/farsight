"""Synthetic unique-point retention/wire benchmark; never claims rig or headset evidence."""

import argparse
import json
import resource
import sys
import time

from jetson.mapping.compress import encode
from jetson.mapping.dense import DenseMap, unpack
from .dense_map import MacCloud


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", type=int, default=2_000_000)
    args = parser.parse_args()
    if not 1 <= args.points <= 5_000_000:
        parser.error("--points must be between 1 and 5000000")
    # Frozen time isolates capacity from TTL. Real unsegmented RGB-D expires at 30s.
    jetson = DenseMap(cache_only=True, clock=lambda: 0)
    mac = MacCloud(clock=lambda: 0)
    started, raw_bytes, compressed_bytes, cursor = time.monotonic(), 0, 0, 0
    for start in range(0, args.points, 8192):
        records = [(i % 500 * 10, i // 500 % 250 * 10, i // 125000 * 10,
                    i % 256, i // 256 % 256, i // 65536 % 256, 1)
                   for i in range(start, min(args.points, start + 8192))]
        jetson.apply(records)
        body = jetson.delta(jetson.epoch, cursor)
        metadata, rows = unpack(body)
        mac.apply(rows)
        cursor = metadata["cursor"]
        raw_bytes += len(body); compressed_bytes += len(encode("br", body))
    elapsed = time.monotonic() - started
    manifest = mac.manifest([2.5, 1.25, 0])
    displayed = 0
    for chunk in manifest["chunks"]:
        meta, _ = unpack(mac.view_chunk(tuple(map(int, chunk["id"].split("_"))), chunk["lod"]))
        displayed += meta["points"]
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(json.dumps(dict(source="SYNTHETIC_UNIQUE_GRID", frozen_ttl=True,
                         observations=args.points, jetson_unique=len(jetson.cells), mac_unique=len(mac.cells),
                         view_selected=displayed, view_chunks=len(manifest["chunks"]),
                         accumulation_s=round(elapsed, 3), raw_payload_bytes=raw_bytes,
                         brotli_payload_bytes=compressed_bytes,
                         payload_bytes_per_s=round(compressed_bytes / elapsed, 1),
                         peak_rss_mib=round(rss / (1024**2 if sys.platform == "darwin" else 1024), 1),
                         headset_fps=None, tailnet_live_bytes_per_s=None), indent=2))


if __name__ == "__main__":
    main()
