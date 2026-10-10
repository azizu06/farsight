"""Consume the Rust assembler's published v1 geometry records, without synthesis."""

from collections import Counter
import struct
import uuid

import numpy as np

HEADER = struct.Struct("<BBHQQ16s16s16fII")


def decode_geometry(body):
    """Concatenated raw v1 records; coordinates are world-baked by the assembler."""
    offset, updates = 0, []
    while offset < len(body):
        if offset + HEADER.size > len(body):
            raise ValueError("Truncated geometry record")
        values = HEADER.unpack_from(body, offset)
        version, op, flags, seq, epoch, session, anchor = values[:7]
        transform = np.array(values[7:23], np.float64).reshape(4, 4, order="F")
        vertices, indices = values[23:]
        size = HEADER.size + vertices * 12 + indices * 4
        if (version != 1 or op not in (0, 1, 2, 3, 4) or flags != 0
                or vertices > 65536 or indices > 65536 or size > 256 * 1024
                or offset + size > len(body) or not np.allclose(transform, np.eye(4))):
            raise ValueError("Invalid world-baked geometry record")
        if op == 0 and (not vertices or not indices or indices % 3):
            raise ValueError("Replace requires a measured mesh")
        if op != 0 and (vertices or indices):
            raise ValueError("Control geometry record contains vertices")
        xyz = np.frombuffer(body, "<f4", vertices * 3, offset + HEADER.size).reshape(-1, 3)
        faces = np.frombuffer(body, "<u4", indices, offset + HEADER.size + vertices * 12)
        if not np.isfinite(xyz).all() or np.any(np.abs(xyz) >= 10000) or np.any(faces >= vertices):
            raise ValueError("Invalid geometry vertex/index")
        update = (str(uuid.UUID(bytes=session)), epoch, seq, op, anchor, xyz.copy())
        if op == 0 and updates and updates[-1][:5] == update[:5]:
            # The assembler fragments oversized replacements using the same
            # (session, epoch, seq, anchor). Apply one complete replacement.
            previous = updates.pop()
            update = (*update[:5], np.concatenate((previous[5], update[5])))
        updates.append(update)
        offset += size
    return updates


class GeometryConsumer:
    """Anchor replacement/removal with reference counts for shared fine cells.

    Gray mesh vertices remain a separate source from calibrated RGB-D colors.
    Reset/snapshot batches are applied under the cloud lock, never partially shown.
    """
    def __init__(self, cloud):
        self.cloud = cloud
        self.identity = None
        self.after = 0
        self.anchors = {}
        self.references = Counter()

    def apply(self, updates, bootstrap=False):
        bootstrap = bootstrap or bool(updates and updates[0][3] == 2)
        with self.cloud.lock:
            for session, epoch, seq, op, anchor, xyz in updates:
                identity = (session, epoch)
                if identity != self.identity or op == 2:
                    self.cloud.clear(dict(kind="arkit_mesh_neutral", session=session, epoch=epoch))
                    self.identity, self.after = identity, 0
                    self.anchors.clear(); self.references.clear()
                if seq <= self.after and op != 2 and not bootstrap:
                    continue
                if op == 2:
                    continue  # a synthetic Reset seq is not the new producer cursor
                self.after = max(self.after, seq)
                if op in (3, 4):
                    # Publisher budget eviction never removes Mac-retained detail.
                    continue
                old = self.anchors.pop(anchor, set())
                mm = np.floor(xyz * 1000).astype(np.int32)
                keys = {tuple(map(int, row)) for row in np.floor_divide(mm, 10)} if op == 0 else set()
                for key in old - keys:
                    self.references[key] -= 1
                    if self.references[key] <= 0:
                        del self.references[key]
                        self.cloud._delete(key)
                for key in keys - old:
                    self.references[key] += 1
                if op == 0:
                    self.anchors[anchor] = keys
                    self.cloud.integrate(xyz, np.full((len(xyz), 3), 160, np.uint8))
