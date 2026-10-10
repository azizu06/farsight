"""Downloader integrity tests against a local HTTP server; no inference claims."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from tools.setup_identity_models import FileSpec, download_verified


PAYLOAD = b"hello world\n"
SHA256 = "a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447"


@contextmanager
def fixture_server(body, content_length=True, status=200):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            if content_length:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fixture.onnx"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class IdentityModelsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.destination = Path(self.directory.name) / "models with spaces"

    def tearDown(self):
        self.directory.cleanup()

    def spec(self, url):
        return FileSpec("fixture.onnx", url, 12, SHA256)

    def assert_no_partial(self):
        self.assertEqual(list(self.destination.glob("*.part")), [])

    def test_verified_download_is_published_and_reusable_without_network(self):
        with fixture_server(PAYLOAD) as url:
            target = download_verified(self.spec(url), self.destination)
        self.assertEqual(target.read_bytes(), b"hello world\n")
        self.assert_no_partial()
        # The HTTP server is gone: a matching installed file must remain usable.
        self.assertEqual(download_verified(self.spec(url), self.destination), target)

    def test_hash_mismatch_preserves_previous_file_and_removes_partial(self):
        self.destination.mkdir()
        target = self.destination / "fixture.onnx"
        target.write_bytes(b"previous operator file")
        with fixture_server(b"HELLO WORLD\n") as url:
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                download_verified(self.spec(url), self.destination)
        self.assertEqual(target.read_bytes(), b"previous operator file")
        self.assert_no_partial()

    def test_oversized_stream_without_content_length_is_not_published(self):
        with fixture_server(PAYLOAD + b"extra", content_length=False) as url:
            with self.assertRaisesRegex(ValueError, "size"):
                download_verified(self.spec(url), self.destination)
        self.assertFalse((self.destination / "fixture.onnx").exists())
        self.assert_no_partial()

    def test_short_stream_without_content_length_is_not_published(self):
        with fixture_server(b"short", content_length=False) as url:
            with self.assertRaisesRegex(ValueError, "size"):
                download_verified(self.spec(url), self.destination)
        self.assertFalse((self.destination / "fixture.onnx").exists())
        self.assert_no_partial()

    def test_wrong_content_length_is_rejected_and_previous_file_is_preserved(self):
        self.destination.mkdir()
        target = self.destination / "fixture.onnx"
        target.write_bytes(b"old")
        with fixture_server(b"short") as url:
            with self.assertRaisesRegex(ValueError, "size"):
                download_verified(self.spec(url), self.destination)
        self.assertEqual(target.read_bytes(), b"old")
        self.assert_no_partial()

    def test_http_failure_is_not_published(self):
        with fixture_server(b"not a model", status=404) as url:
            with self.assertRaises(OSError):
                download_verified(self.spec(url), self.destination)
        self.assertFalse((self.destination / "fixture.onnx").exists())
        self.assert_no_partial()

    def test_corrupted_installed_file_is_replaced_with_verified_download(self):
        self.destination.mkdir()
        target = self.destination / "fixture.onnx"
        target.write_bytes(b"HELLO WORLD\n")
        with fixture_server(PAYLOAD) as url:
            download_verified(self.spec(url), self.destination)
        self.assertEqual(target.read_bytes(), b"hello world\n")
        self.assert_no_partial()


if __name__ == "__main__":
    unittest.main()
