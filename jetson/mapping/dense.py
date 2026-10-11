"""Measured visual detail, independent of the navigation voxel/mesh budgets.

Wire: u32 JSON length, UTF-8 metadata, then 16-byte records (<iiiBBBB):
millimetre xyz, RGB, operation (1 upsert, 0 delete). No synthesized samples.
All samples expire: an unsegmented moving body must never become permanent.
"""

from collections import OrderedDict, deque
import base64
from bisect import bisect_right
from itertools import islice
import json
import struct
import threading
import time
import uuid

import numpy as np

RECORD = struct.Struct("<iiiBBBB")
_RECORD_DTYPE = np.dtype([("xyz", "<i4", (3,)), ("rgb", "u1", (3,)), ("op", "u1")])
MAX_RECORDS = 8192


def packet(metadata, records):
    header = json.dumps(metadata, separators=(",", ":")).encode()
    return struct.pack("<I", len(header)) + header + b"".join(records)


def unpack(body):
    if len(body) < 4:
        raise ValueError("Truncated dense header")
    size = struct.unpack_from("<I", body)[0]
    if not 0 < size <= 65536 or size + 4 > len(body):
        raise ValueError("Invalid dense header")
    metadata = json.loads(body[4:4 + size])
    data = body[4 + size:]
    if metadata.get("format") != "xyz_mm_rgb8_op" or len(data) % RECORD.size:
        raise ValueError("Invalid dense records")
    if len(data) // RECORD.size > MAX_RECORDS:
        raise ValueError("Dense chunk exceeds record budget")
    records = list(RECORD.iter_unpack(data))
    if any(row[6] not in (0, 1) or any(abs(v) >= 10_000_000 for v in row[:3]) for row in records):
        raise ValueError("Invalid dense operation or coordinate")
    return metadata, records


class DenseMap:
    """Fine cells + bounded change journal. Slow clients bootstrap in bounded pages.

    Jetson keeps 600k cells; the Mac can keep five million. A revision belongs
    only to its epoch. Snapshot pages use the active cell revision order, so
    concurrent observations move forward and cannot be silently skipped.
    """

    def __init__(self, capacity=600_000, ttl=30, journal_size=200_000, clock=time.monotonic,
                 cache_only=False):
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self.cache_only = cache_only
        self.lock = threading.RLock()
        self.cells = OrderedDict()
        self.journal = deque(maxlen=journal_size)
        self.revision = 0
        self.epoch = uuid.uuid4().hex
        self.source = None
        self.snapshots = OrderedDict()
        self.snapshot_times = {}
        self.depth_view = None
        self.view_key = None
        self.view_received = 0.
        self.view_revision = 0
        self.authoritative_deletes = set()
        self.revisited_cells = 0

    def clear(self, source=None):
        with self.lock:
            self.cells.clear()
            self.journal.clear()
            self.revision = 0
            self.epoch = uuid.uuid4().hex
            self.source = source
            self.snapshots.clear()
            self.snapshot_times.clear()
            self.depth_view, self.view_key = None, None
            self.authoritative_deletes.clear()

    def _change(self, record):
        if self.journal and len(self.journal) == self.journal.maxlen:
            self.authoritative_deletes.discard(self.journal[0][0])
        self.revision += 1
        self.journal.append((self.revision, record))
        return self.revision

    def _delete(self, key, authoritative=False):
        self.cells.pop(key, None)
        self._change(RECORD.pack(*(v * 10 for v in key), 0, 0, 0, 0))
        if authoritative:
            self.authoritative_deletes.add(self.revision)

    def publish_view(self, view, received_at=None):
        """Carry native revisit evidence independently of coalesced point changes."""
        metadata = view.to_metadata()
        key = json.dumps(metadata["sensor_key"], separators=(",", ":"))
        with self.lock:
            self.depth_view, self.view_key = metadata, key
            self.view_received = self.clock() if received_at is None else received_at
            self.view_revision = self.revision

    def reconcile(self, view, *, keys=None, protected=()):
        """Delete contradicted foreground, with projection outside the cloud lock.

        The Mac's spatial chunks select only the current view's possible region.
        A cached cell updated during projection is retained for the next view.
        """
        far = float(view.depth_mm.max()) * .001
        if not far:
            return 0
        k, pose = view.calibration, view.transform
        rays = np.asarray([[(u - k.cx) * far / k.fx, -(v - k.cy) * far / k.fy, -far]
                           for u in (-.5, view.width - .5) for v in (-.5, view.height - .5)])
        corners = np.vstack((rays @ pose[:3, :3].T + pose[:3, 3], pose[:3, 3]))
        low, high = np.floor(corners.min(axis=0) * .5).astype(int), np.floor(corners.max(axis=0) * .5).astype(int)
        with self.lock:
            epoch = self.epoch
            if keys is not None:
                keys = list(keys)
            elif hasattr(self, "chunks"):
                keys = [key for chunk, members in self.chunks.items()
                        if all(low[i] <= chunk[i] <= high[i] for i in range(3)) for key in members]
            else:
                keys = list(self.cells)
            entries = [(key, self.cells[key]) for key in keys if key in self.cells and key not in protected]
        if not entries:
            return 0
        records = np.frombuffer(b"".join(value[1] for _, value in entries), _RECORD_DTYPE)
        removed = np.flatnonzero(view.contradicted(records["xyz"].astype(np.float64) * .001))
        with self.lock:
            if self.epoch != epoch:
                return 0
            count = 0
            for index in removed:
                key, value = entries[index]
                if self.cells.get(key) is value:
                    self._delete(key, authoritative=True)
                    count += 1
            self.revisited_cells += count
            return count

    def expire(self):
        with self.lock:
            for saved in list(self.snapshots):
                if self.clock() - self.snapshot_times[saved] > 30:
                    del self.snapshots[saved]
                    del self.snapshot_times[saved]
            cutoff = self.clock() - self.ttl
            # cells are ordered by last wire refresh, at most 5 s behind receipt.
            while self.cells:
                key, value = next(iter(self.cells.items()))
                if value[2] > cutoff:
                    break
                self._delete(key)

    def integrate(self, points, colors):
        if not len(points):
            return
        valid = np.isfinite(points).all(axis=1) & (np.abs(points) < 10000).all(axis=1)
        xyz = np.floor(points[valid] * 1000).astype(np.int32)
        rgb = np.asarray(colors[valid], np.uint8)
        # Retain the first real sample per cell. Fixed-size keys avoid NumPy's
        # expensive row comparison, but their byte order is not numeric order.
        # Restore the original signed xyz lexicographic order for revisions,
        # wire pages and capacity eviction, including negative coordinates.
        keys = np.ascontiguousarray(np.floor_divide(xyz, 10))
        _, keep = np.unique(keys.view(np.dtype((np.void, 12))).reshape(-1), return_index=True)
        selected = keys[keep]
        keep = keep[np.lexsort((selected[:, 2], selected[:, 1], selected[:, 0]))]
        records = np.empty(len(keep), dtype=_RECORD_DTYPE)
        records["xyz"], records["rgb"], records["op"] = xyz[keep], rgb[keep], 1
        self.apply(RECORD.iter_unpack(records.tobytes()))

    def apply(self, records):
        with self.lock:
            now = self.clock()
            self.expire()
            for record in records:
                key = tuple(v // 10 for v in record[:3])
                if record[6] == 0:
                    if key in self.cells:
                        self._delete(key)
                    continue
                if record[6] != 1:
                    raise ValueError("Unknown dense operation")
                old = self.cells.get(key)
                encoded = RECORD.pack(*record)
                # Coalesce noisy refreshes; still renew remote TTL every 5 s.
                if old and old[1] == encoded and now - old[2] < 5:
                    continue
                rev = self._change(encoded)
                self.cells[key] = (rev, encoded, now)
                self.cells.move_to_end(key)
            while len(self.cells) > self.capacity:
                key = next(iter(self.cells))
                if self.cache_only:
                    self.cells.pop(key)  # Mac retains points beyond Jetson's cache budget.
                else:
                    self._delete(key)

    def delta(self, epoch=None, after=0, snapshot=False, limit=MAX_RECORDS, anchor=None, *, view_after=None):
        with self.lock:
            self.expire()
            limit = max(1, min(limit, MAX_RECORDS))
            reset = epoch != self.epoch or after > self.revision
            if not snapshot and self.journal and after < self.journal[0][0] - 1:
                reset = True
            if snapshot and anchor is not None and anchor not in self.snapshots:
                reset = True
            if reset:
                after, snapshot = 0, True
                anchor = self.revision
                # Immutable cell tuples let pages seek in O(log N + page), rather
                # than rescan millions of cells for every 8192-point page.
                self.snapshots[anchor] = list(self.cells.values())
                self.snapshot_times[anchor] = self.clock()
                while len(self.snapshots) > 2:
                    stale, _ = self.snapshots.popitem(last=False)
                    del self.snapshot_times[stale]
            if snapshot and anchor is None:
                raise ValueError("Snapshot continuation requires its journal anchor")
            if snapshot:
                entries = self.snapshots[anchor]
                start = bisect_right(entries, after, key=lambda v: v[0])
                rows = [(v[0], v[1]) for v in entries[start:start + limit + 1]]
            else:
                # Revisions are contiguous. Copy one bounded page instead of
                # allocating/reversing the entire outstanding 200k journal.
                start = max(0, after - self.journal[0][0] + 1) if self.journal else 0
                rows = list(islice(self.journal, start, start + limit + 1))
            more = len(rows) > limit
            rows = rows[:limit]
            cursor = rows[-1][0] if more else anchor if snapshot else self.revision
            metadata = dict(format="xyz_mm_rgb8_op", epoch=self.epoch,
                               source=self.source, cursor=cursor, reset=reset,
                               snapshot=snapshot and more, more=more, anchor=anchor,
                               points=len(self.cells), voxel_m=.01, ttl_s=self.ttl)
            if self.depth_view is not None and (reset or self.view_key != view_after):
                metadata["view"] = dict(self.depth_view, key=self.view_key,
                                        age_ms=round(max(0, self.clock() - self.view_received) * 1000, 1))
            if self.authoritative_deletes:
                mask = np.asarray([revision in self.authoritative_deletes for revision, _ in rows], np.uint8)
                if mask.any():
                    metadata["authoritative_deletes"] = base64.b64encode(
                        np.packbits(mask, bitorder="little").tobytes()).decode("ascii")
            if self.depth_view is not None:
                newer = np.asarray([revision > self.view_revision and record[-1] == 1
                                    for revision, record in rows], np.uint8)
                if newer.any():
                    metadata["newer_than_view"] = base64.b64encode(
                        np.packbits(newer, bitorder="little").tobytes()).decode("ascii")
            return packet(metadata, [record for _, record in rows])
