import gzip
import unittest

import brotli
import numpy as np
from fastapi.testclient import TestClient

from jetson.mapping.compress import EncodedCache, negotiate
from jetson.mapping.server import create_app


class NegotiateTests(unittest.TestCase):
    def test_prefers_brotli_then_gzip(self):
        self.assertEqual(negotiate("gzip, deflate, br"), "br")
        self.assertEqual(negotiate("gzip"), "gzip")
        self.assertEqual(negotiate("BR;q=0.5, gzip;q=1"), "br")

    def test_refusals_and_absence(self):
        self.assertIsNone(negotiate(None))
        self.assertIsNone(negotiate("identity"))
        self.assertIsNone(negotiate("br;q=0, gzip;q=0"))
        self.assertEqual(negotiate("br;q=0, *"), "gzip")
        self.assertEqual(negotiate("br;q=bad, gzip"), "gzip")


class CacheTests(unittest.TestCase):
    def test_encodes_once_per_version_and_decodes_identically(self):
        raw = np.arange(20000, dtype=np.float32).tobytes()
        builds, version = [], [1]

        def build():
            builds.append(1)
            return version[0], raw
        cache = EncodedCache()
        body, coding = cache.get("p", lambda: version[0], build, "br, gzip")
        self.assertEqual(coding, "br")
        self.assertLess(len(body), len(raw))
        self.assertEqual(brotli.decompress(body), raw)
        again, _ = cache.get("p", lambda: version[0], build, "br")
        self.assertIs(again, body)
        gz, coding = cache.get("p", lambda: version[0], build, "gzip")
        self.assertEqual((coding, gzip.decompress(gz)), ("gzip", raw))
        self.assertEqual(len(builds), 1)
        plain, coding = cache.get("p", lambda: version[0], build, None)
        self.assertEqual((plain, coding), (raw, None))
        version[0] = 2
        cache.get("p", lambda: version[0], build, "br")
        self.assertEqual(len(builds), 2)

    def test_tiny_bodies_stay_identity(self):
        _, coding = EncodedCache().get("p", lambda: 1, lambda: (1, b"\0" * 8), "br, gzip")
        self.assertIsNone(coding)


class RouteTests(unittest.TestCase):
    def test_map_routes_negotiate_and_decode_identical(self):
        app = create_app(demo=True)
        with TestClient(app) as client:
            for path in ("/map/points.bin", "/map/mesh.bin", "/map/export.ply"):
                plain = client.get(path, headers={"Accept-Encoding": "identity"})
                self.assertNotIn("content-encoding", plain.headers)
                self.assertEqual(plain.headers["vary"], "Accept-Encoding")
                for coding, decode in (("br", brotli.decompress), ("gzip", gzip.decompress)):
                    with client.stream("GET", path, headers={"Accept-Encoding": coding}) as r:
                        wire = b"".join(r.iter_raw())
                        self.assertEqual(r.headers.get("content-encoding"), coding if len(plain.content) >= 1024 else None)
                        self.assertEqual(r.headers["vary"], "Accept-Encoding")
                    self.assertEqual(decode(wire) if len(plain.content) >= 1024 else wire, plain.content)


if __name__ == "__main__":
    unittest.main()
