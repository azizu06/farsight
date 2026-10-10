"""br/gzip content-encoding for the large map routes, encoded once per map version."""

import gzip
import threading

try:
    import brotli
except ImportError:  # optional: fall back to gzip only
    brotli = None

MIN_BYTES = 1024
BROTLI_QUALITY = 4  # q4 keeps 600k-point maps well under a second; higher qualities cost seconds
GZIP_LEVEL = 5


def negotiate(accept_encoding):
    """Pick br, gzip or None from an Accept-Encoding header (q=0 refuses a coding)."""
    offered = {}
    for part in (accept_encoding or "").split(","):
        name, _, params = part.strip().partition(";")
        name, q = name.strip().lower(), 1.0
        params = params.strip().replace(" ", "")
        if params.startswith("q="):
            try:
                q = float(params[2:])
            except ValueError:
                q = 0.0
        if name:
            offered[name] = q
    wildcard = offered.get("*", 0.0)
    for coding in (("br",) if brotli else ()) + ("gzip",):
        if offered.get(coding, wildcard) > 0:
            return coding
    return None


def encode(coding, body):
    if coding == "br":
        return brotli.compress(body, quality=BROTLI_QUALITY)
    return gzip.compress(body, compresslevel=GZIP_LEVEL, mtime=0)


class EncodedCache:
    """Keeps the newest (version, body, {coding: bytes}) per route; a new version drops the old one."""

    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def get(self, route, peek, build, accept_encoding):
        """Return (body, coding). peek() is the cheap current map version; build() -> (version, raw bytes)
        runs only when the cached version is stale."""
        coding = negotiate(accept_encoding)
        with self.lock:
            entry = self.entries.get(route)
            if entry is None or entry[0] != peek():
                version, raw = build()
                entry = self.entries[route] = (version, raw, {})
            _, raw, variants = entry
            if coding is None or len(raw) < MIN_BYTES:
                return raw, None
            if coding not in variants:
                variants[coding] = encode(coding, raw)
            return variants[coding], coding
