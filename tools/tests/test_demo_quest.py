"""Quest USB launcher boundaries; fake ADB processes never touch hardware."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools import demo


class QuestLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="quest adb ")
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.adb = root / "adb.exe"
        self.calls = root / "calls.jsonl"
        # A process double is needed because no physical Quest is attached.
        # It records the boundary commands, including each argument's identity.
        self.adb.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            "time.sleep(float(os.environ.get('QUEST_TEST_STARTUP_DELAY', '0')))\n"
            "with open(os.environ['QUEST_TEST_CALLS'], 'a') as calls:\n"
            "    calls.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if 'get-state' in sys.argv:\n"
            "    if os.environ.get('QUEST_TEST_UNAUTHORIZED'):\n"
            "        print('error: device unauthorized', file=sys.stderr)\n"
            "        sys.exit(1)\n"
            "    time.sleep(float(os.environ.get('QUEST_TEST_DELAY', '0')))\n"
            "    print('device')\n"
        )
        self.adb.chmod(0o700)
        environment = patch.dict(os.environ, {"QUEST_TEST_CALLS": str(self.calls)})
        environment.start()
        self.addCleanup(environment.stop)

    def commands(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_explicit_executable_with_spaces_targets_one_headset(self):
        # Catches using PATH's Linux adb instead of the requested Windows adb,
        # shell splitting the executable path, or dropping the serial.
        demo.quest_reverse(8080, "QUEST-2", adb=str(self.adb))
        self.assertEqual(self.commands(), [
            ["-s", "QUEST-2", "get-state"],
            ["-s", "QUEST-2", "reverse", "tcp:8080", "tcp:8080"],
            ["-s", "QUEST-2", "reverse", "--list"],
        ])

    def test_unauthorized_headset_never_creates_a_tunnel(self):
        with patch.dict(os.environ, {"QUEST_TEST_UNAUTHORIZED": "1"}):
            with self.assertRaises(subprocess.CalledProcessError):
                demo.quest_reverse(8080, None, adb=str(self.adb))
        self.assertEqual(self.commands(), [["get-state"]])

    def test_unresponsive_adb_fails_instead_of_hanging_launcher(self):
        # A slow host may time out before Python creates the fake's log.
        # Both startup-time and already-running timeouts must fail before reverse.
        for startup_delay in ("0", "5"):
            with self.subTest(startup_delay=startup_delay):
                self.calls.unlink(missing_ok=True)
                started = time.monotonic()
                with patch.dict(os.environ, {"QUEST_TEST_DELAY": "5",
                                            "QUEST_TEST_STARTUP_DELAY": startup_delay}):
                    with self.assertRaisesRegex(RuntimeError, "timed out") as failure:
                        demo.quest_reverse(8080, None, adb=str(self.adb), timeout=0.15)
                self.assertLess(time.monotonic() - started, 2)
                cause = failure.exception.__cause__
                self.assertIsInstance(cause, subprocess.TimeoutExpired)
                self.assertEqual(cause.cmd, [str(self.adb), "get-state"])
                commands = self.commands() if self.calls.exists() else []
                self.assertIn(commands, ([], [["get-state"]]))

    def test_check_accepts_explicit_adb_without_probing_usb(self):
        # The prerequisite check must not depend on ignored build output in
        # the developer's checkout. Supply a minimal built-app fixture while
        # still exercising the real parser in a separate process.
        fixture_root = Path(self.directory.name) / "repo"
        built_app = fixture_root / "quest/dist/index.html"
        built_app.parent.mkdir(parents=True)
        built_app.write_text("<!doctype html><title>Quest fixture</title>")
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            port = port_probe.getsockname()[1]
        result = subprocess.run(
            [sys.executable, "-c",
             "from pathlib import Path; import sys; from tools import demo; "
             "demo.ROOT = Path(sys.argv[1]); sys.argv = sys.argv[2:]; demo.main()",
             str(fixture_root), "tools.demo", "--upstream", "http://127.0.0.1:18443",
             "--quest", "--adb", str(self.adb), "--port", str(port), "--check"],
            cwd=demo.ROOT, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Prerequisites ready", result.stdout)
        self.assertFalse(self.calls.exists(), "--check must not start/probe ADB")


if __name__ == "__main__":
    unittest.main()
