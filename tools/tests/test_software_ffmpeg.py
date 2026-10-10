"""Mock ARM/apt fixture tests; they do not qualify a Jetson encoder."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "jetson/install_software_ffmpeg.sh"
VERSION = "7:6.1.1-3ubuntu5"


class SoftwareFFmpegTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="farsight ffmpeg ")
        self.root = Path(self.directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.prefix = self.root / "private prefix"
        self.wrapper = self.root / "user bin" / "ffmpeg"
        self.log = self.root / "commands.jsonl"
        self.env = {**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                    "FFMPEG_FIXTURE_LOG": str(self.log), "FFMPEG_FIXTURE_PREFIX": str(self.prefix.resolve())}
        self.program("uname", "#!/bin/sh\n[ \"$1\" != -s ] || { echo Linux; exit; }; echo aarch64\n")
        self.program("dpkg", "#!/bin/sh\necho arm64\n")
        self.program("timeout", "#!/bin/sh\nshift 2\nexec \"$@\"\n")
        self.program("apt-get", """#!/usr/bin/env python3
import json,os,pathlib,sys
with open(os.environ['FFMPEG_FIXTURE_LOG'],'a') as log: log.write(json.dumps(sys.argv)+'\\n')
if os.environ.get('FAIL_DOWNLOAD'): sys.exit(17)
assert sys.argv[1]=='download'
for specification in sys.argv[2:]:
 name,_,version=specification.partition('=')
 pathlib.Path(name+'_fixture.deb').write_text(json.dumps({'Package':name,'Version':version or 'fixture','Architecture':os.environ.get('PACKAGE_ARCH','arm64')}))
""")
        self.program("dpkg-deb", """#!/usr/bin/env python3
import json,os,pathlib,sys
with open(os.environ['FFMPEG_FIXTURE_LOG'],'a') as log: log.write(json.dumps(sys.argv)+'\\n')
if sys.argv[1]=='--field':
 print(json.loads(pathlib.Path(sys.argv[2]).read_text())[sys.argv[3]]);sys.exit()
assert sys.argv[1]=='-x'
metadata=json.loads(pathlib.Path(sys.argv[2]).read_text());root=pathlib.Path(sys.argv[3])
(root/'usr/lib/aarch64-linux-gnu').mkdir(parents=True,exist_ok=True)
if metadata['Package']=='ffmpeg':
 target=root/'usr/bin/ffmpeg';target.parent.mkdir(parents=True,exist_ok=True)
 target.write_text(pathlib.Path(os.environ['FFMPEG_FIXTURE_BINARY']).read_text());target.chmod(0o755)
""")
        binary = self.root / "mock ffmpeg.py"
        binary.write_text("""#!/usr/bin/env python3
import json,os,pathlib,sys
with open(os.environ['FFMPEG_FIXTURE_LOG'],'a') as log: log.write(json.dumps({'ffmpeg':sys.argv,'library_path':os.environ.get('LD_LIBRARY_PATH')})+'\\n')
if '-encoders' in sys.argv:
 print(' V....D '+('other' if os.environ.get('MISSING_X264') else 'libx264')+' encoder');sys.exit()
if os.environ.get('FAIL_ENCODE'): sys.exit(4)
if os.environ.get('FAIL_FINAL') and str(pathlib.Path(__file__)).startswith(os.environ['FFMPEG_FIXTURE_PREFIX']): sys.exit(5)
pathlib.Path(sys.argv[-1]).write_bytes(b'fixture mp4 output, not a real encoder')
""")
        self.env["FFMPEG_FIXTURE_BINARY"] = str(binary)
        self.program("sudo", """#!/usr/bin/env python3
import json,os,pathlib,sys
with open(os.environ['FFMPEG_FIXTURE_LOG'],'a') as log:
 log.write(json.dumps({'sudo':sys.argv[1:]})+'\\n')
 if sys.argv[1]=='install': log.write(json.dumps({'dropin':pathlib.Path(sys.argv[-2]).read_text()})+'\\n')
""")

    def tearDown(self):
        self.directory.cleanup()

    def program(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def run_helper(self, *args, env=None):
        return subprocess.run(["bash", str(HELPER), "--prefix", str(self.prefix), "--wrapper", str(self.wrapper), *args],
                              cwd=ROOT, env={**self.env, **(env or {})}, text=True, capture_output=True, timeout=20)

    def events(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_download_extract_verify_and_rerun_do_not_install_or_replace_system_libraries(self):
        first = self.run_helper()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("encoder verified", first.stdout)
        self.assertTrue(self.wrapper.is_file())
        events = self.events()
        downloads = [event for event in events if isinstance(event, list) and Path(event[0]).name == "apt-get"]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0][1], "download")
        self.assertIn("ffmpeg=" + VERSION, downloads[0])
        self.assertNotIn("install", downloads[0])
        self.assertFalse(any(isinstance(event, dict) and "sudo" in event for event in events))
        calls = [event for event in events if isinstance(event, dict) and "ffmpeg" in event]
        self.assertTrue(any("libx264" in event["ffmpeg"] for event in calls))
        self.assertTrue(calls[-1]["library_path"].startswith(str(self.prefix.resolve() / "usr/lib/aarch64-linux-gnu")))
        before = len(downloads)
        again = self.run_helper()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("reusing", again.stdout)
        self.assertEqual(len([event for event in self.events() if isinstance(event, list) and Path(event[0]).name == "apt-get"]), before)

    def test_failed_encoder_never_publishes_or_claims_ready(self):
        for failure in ("MISSING_X264", "FAIL_ENCODE", "FAIL_DOWNLOAD"):
            with self.subTest(failure=failure):
                result = self.run_helper(env={failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("encoder verified", result.stdout)
                self.assertFalse(self.prefix.exists())
                self.assertFalse(self.wrapper.exists())
                self.assertEqual(list(self.root.glob(".farsight-ffmpeg.*")), [])

    def test_wrong_architecture_is_rejected_before_publishing(self):
        result = self.run_helper(env={"PACKAGE_ARCH": "amd64"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Wrong package architecture", result.stderr)
        self.assertFalse(self.prefix.exists())

    def test_offline_package_directory_is_complete_and_requires_matching_version(self):
        packages = self.root / "offline packages"
        packages.mkdir()
        names = ("ffmpeg", "libavcodec60", "libavdevice60", "libavfilter9", "libavformat60", "libavutil58",
                 "libswresample4", "libswscale7", "libpostproc57", "libx264-164")
        for name in names:
            (packages / (name + ".deb")).write_text(json.dumps(dict(Package=name, Architecture="arm64", Version=VERSION)))
        result = self.run_helper("--package-dir", str(packages))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(isinstance(event, list) and Path(event[0]).name == "apt-get" for event in self.events()))
        shutil.rmtree(self.prefix)
        self.wrapper.unlink()
        (packages / "libavcodec60.deb").unlink()
        result = self.run_helper("--package-dir", str(packages))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Required extracted package missing", result.stderr)
        self.assertFalse(self.prefix.exists())

    def test_final_failure_restores_previous_private_prefix_and_wrapper(self):
        original = self.prefix / "usr/bin/ffmpeg"
        original.parent.mkdir(parents=True)
        original.write_text("#!/bin/sh\nexit 1\n")
        original.chmod(0o755)
        self.wrapper.parent.mkdir()
        self.wrapper.write_text("old user wrapper")
        result = self.run_helper(env={"FAIL_FINAL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("rolling back", result.stderr)
        self.assertEqual(original.read_text(), "#!/bin/sh\nexit 1\n")
        self.assertEqual(self.wrapper.read_text(), "old user wrapper")

    def test_system_paths_symlink_and_unknown_existing_prefix_are_refused(self):
        for target in ("/usr/bin/ffmpeg", "/etc/ffmpeg"):
            result = self.run_helper("--dry-run", "--wrapper", target)
            self.assertEqual(result.returncode, 2)
        self.prefix.mkdir()
        (self.prefix / "unrelated.txt").write_text("do not replace")
        self.assertNotEqual(self.run_helper().returncode, 0)
        self.assertEqual((self.prefix / "unrelated.txt").read_text(), "do not replace")

    def test_service_dropin_is_opt_in_and_never_restarts_service(self):
        result = self.run_helper("--install-service-path")
        self.assertEqual(result.returncode, 0, result.stderr)
        events = self.events()
        calls = [event["sudo"] for event in events if isinstance(event, dict) and "sudo" in event]
        self.assertEqual(calls[-1], ["systemctl", "daemon-reload"])
        self.assertFalse(any("restart" in command for command in calls))
        dropin = next(event["dropin"] for event in events if isinstance(event, dict) and "dropin" in event)
        self.assertIn("PATH=" + str(self.wrapper.parent), dropin)
        self.assertIn("/usr/local/cuda/bin", dropin)

    def test_dry_run_changes_nothing_and_setup_hook_is_opt_in(self):
        result = self.run_helper("--dry-run", "--install-service-path")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("apt-get download", result.stdout)
        self.assertFalse(self.prefix.exists())
        self.assertFalse(self.wrapper.exists())
        self.assertFalse(self.log.exists())
        setup = subprocess.run(["bash", str(ROOT / "jetson/setup.sh"), "--dry-run", "--core-only", "--skip-engines", "--with-software-ffmpeg"],
                               cwd=ROOT, text=True, capture_output=True, timeout=20)
        self.assertEqual(setup.returncode, 0, setup.stderr)
        self.assertIn("private prefix", setup.stdout)
        self.assertIn("no automatic restart", setup.stdout)


if __name__ == "__main__":
    unittest.main()
