"""Core-first provisioning must avoid optional work and preserve remote edits."""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def run(*args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, timeout=20, **kwargs)


class CoreSetupTests(unittest.TestCase):
    def test_core_only_keeps_runtime_and_detector_preparation_without_optional_downloads(self):
        result = run("bash", "jetson/setup.sh", "--dry-run", "--core-only", cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nvidia-jetpack", result.stdout)
        self.assertIn("venv --system-site-packages", result.stdout)
        self.assertIn("TensorRT FP16 engines", result.stdout)
        self.assertIn("Optional model downloads skipped", result.stdout)
        for optional_work in ("would download", "pip install -q piper-tts", "git clone --depth 1",
                              "cmake --build", "depth_anything"):
            self.assertNotIn(optional_work, result.stdout)

    def test_conflicting_optional_download_request_is_rejected_in_either_order(self):
        for options in (("--core-only", "--with-vlm"), ("--with-vlm", "--core-only")):
            result = run("bash", "jetson/setup.sh", "--dry-run", *options, cwd=ROOT)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("apt-get", result.stdout)

    def test_remote_checkout_with_edits_aborts_before_fetch_or_setup(self):
        # Execute the actual remote program emitted by the supported dry-run.
        # A local dirty repository stands in for the remote checkout; no SSH,
        # apt, network request, model copy, or provisioning takes place.
        preview = run("bash", "tools/bootstrap_jetson.sh", "--dry-run", "--no-models",
                      "fixture@localhost", cwd=ROOT)
        self.assertEqual(preview.returncode, 0, preview.stderr)
        program = preview.stdout.split("<<script\n", 1)[1].split("\n  script\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            self.assertEqual(run("git", "init", "-q", directory).returncode, 0)
            tracked = checkout / "tracked.txt"
            tracked.write_text("original\n")
            self.assertEqual(run("git", "-C", directory, "add", ".").returncode, 0)
            commit = run("git", "-C", directory, "-c", "user.name=Fixture", "-c",
                         "user.email=fixture@example.invalid", "commit", "-qm", "initial")
            self.assertEqual(commit.returncode, 0, commit.stderr)
            tracked.write_text("teammate change\n")
            remote_dir = os.path.relpath(checkout, Path.home())
            result = run("bash", "-s", "--", remote_dir, "unreachable.invalid/repo", "main",
                         input=program, cwd=ROOT)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("stopping bootstrap", result.stdout)
            self.assertEqual(tracked.read_text(), "teammate change\n")
            self.assertNotIn("Could not resolve", result.stderr)


if __name__ == "__main__":
    unittest.main()
