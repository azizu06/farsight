import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from tools import fetch_models


class FakeResponse(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {} if length is False else {"Content-Length": str(len(data) if length is None else length)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opener_for(files):
    calls = []

    def opener(url, timeout=None):
        calls.append(url)
        return FakeResponse(files[url])
    opener.calls = calls
    return opener


class RegistryTests(unittest.TestCase):
    def test_piper_group_has_english_and_spanish_voice_with_configs(self):
        names = {item.name for item in fetch_models.GROUPS["piper"]}
        self.assertEqual(names, {"piper/en_US-lessac-medium.onnx", "piper/en_US-lessac-medium.onnx.json",
                                 "piper/es_MX-claude-high.onnx", "piper/es_MX-claude-high.onnx.json"})

    def test_face_model_is_pinned_by_sha256(self):
        (face,) = fetch_models.GROUPS["face"]
        self.assertEqual(face.name, "version-RFB-320.onnx")
        self.assertEqual(len(face.sha256), 64)
        self.assertIn("6fd293d22b523ec88959f104b8eef5395e3adfbc", face.url)

    def test_default_groups_skip_the_large_vlm(self):
        self.assertNotIn("vlm", fetch_models.DEFAULT_GROUPS)
        self.assertIn("vlm", fetch_models.GROUPS)


class FetchFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def test_downloads_atomically_and_creates_parents(self):
        item = fetch_models.Item("piper/x.onnx", "http://h/x")
        fetch_models.fetch_file(item, self.dir, opener_for({"http://h/x": b"abc"}))
        self.assertEqual((self.dir / "piper/x.onnx").read_bytes(), b"abc")
        self.assertEqual(list(self.dir.rglob("*.part")), [])

    def test_sha_mismatch_leaves_no_file(self):
        item = fetch_models.Item("f.onnx", "http://h/f", sha256="0" * 64)
        with self.assertRaises(fetch_models.FetchError):
            fetch_models.fetch_file(item, self.dir, opener_for({"http://h/f": b"abc"}))
        self.assertFalse((self.dir / "f.onnx").exists())
        self.assertEqual(list(self.dir.rglob("*.part")), [])

    def test_sha_match_accepted(self):
        data = b"model"
        item = fetch_models.Item("f.onnx", "http://h/f", sha256=hashlib.sha256(data).hexdigest())
        fetch_models.fetch_file(item, self.dir, opener_for({"http://h/f": data}))
        self.assertTrue((self.dir / "f.onnx").exists())

    def test_truncated_download_rejected_by_content_length(self):
        item = fetch_models.Item("f.bin", "http://h/f")

        def opener(url, timeout=None):
            return FakeResponse(b"abc", length=10)
        with self.assertRaises(fetch_models.FetchError):
            fetch_models.fetch_file(item, self.dir, opener)
        self.assertFalse((self.dir / "f.bin").exists())

    def test_empty_download_rejected(self):
        item = fetch_models.Item("f.bin", "http://h/f")
        with self.assertRaises(fetch_models.FetchError):
            fetch_models.fetch_file(item, self.dir, opener_for({"http://h/f": b""}))


class MainTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))

    def run_main(self, *argv, files=None):
        out = io.StringIO()
        opener = opener_for(files or {})
        code = fetch_models.main([*argv, "--models-dir", str(self.dir)], opener=opener, out=out)
        return code, out.getvalue(), opener

    def test_check_lists_missing_and_exits_nonzero(self):
        code, text, opener = self.run_main("--check", "--groups", "face")
        self.assertEqual(code, 1)
        self.assertIn("MISSING", text)
        self.assertIn("version-RFB-320.onnx", text)
        self.assertEqual(opener.calls, [])

    def test_dry_run_never_downloads(self):
        code, text, opener = self.run_main("--dry-run", "--groups", "piper")
        self.assertEqual(code, 0)
        self.assertEqual(opener.calls, [])
        self.assertIn("would download", text)

    def test_present_files_are_skipped_so_reruns_are_free(self):
        (self.dir / "piper").mkdir()
        files = {}
        for item in fetch_models.GROUPS["piper"]:
            (self.dir / item.name).write_bytes(b"x")
        code, text, opener = self.run_main("--groups", "piper", files=files)
        self.assertEqual(code, 0)
        self.assertEqual(opener.calls, [])
        self.assertIn("present", text)

    def test_download_failure_continues_and_exits_nonzero(self):
        def broken(url, timeout=None):
            raise OSError("offline")
        out = io.StringIO()
        code = fetch_models.main(["--groups", "face", "--models-dir", str(self.dir)], opener=broken, out=out)
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out.getvalue())

    def test_exported_models_are_reported_not_downloaded(self):
        code, text, opener = self.run_main("--check", "--groups", "export")
        self.assertEqual(code, 1)
        self.assertIn("yolo11n-pose.onnx", text)
        self.assertIn("tools.export_detector", text)

    def test_unknown_group_rejected(self):
        with self.assertRaises(SystemExit):
            self.run_main("--groups", "nope")


if __name__ == "__main__":
    unittest.main()
