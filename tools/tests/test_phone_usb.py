"""USB tool fixtures and real loopback forwarding; no phone or hardware is used."""

from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock

from tools.phone_usb import BridgeError, PhoneUSB, main, require_modern_iproxy, select_usb_device, preflight_port, health_summary


DEVICE = "a" * 40
OTHER_DEVICE = "b" * 40
HELP = """Usage: iproxy [OPTIONS] LOCAL_PORT:DEVICE_PORT
  --local     USB-only device connection
  --source ADDR listening address
  --udid UDID explicit target
"""
LIST_HELP = "--list list USB devices\n--network network devices\n"
HEALTH = dict(camera=True, camera_drive_ready=True, camera_source="PHONE RGB", rover=False,
              detector="pose", detector_backend="pose-tensorrt", video=dict(ready=True),
              control=dict(simulated=False, session_id="secret-controller", permit="secret-permit"),
              replay=False, private_account="hidden-account")


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FixturePrograms:
    def __init__(self, directory, backend_port, exit_early=False, exit_later=False):
        self.marker = Path(directory) / "terminated.txt"
        self.iproxy, self.listing = Path(directory) / "iproxy", Path(directory) / "idevice_id"
        self.listing.write_text(f"#!{sys.executable}\nimport sys\nprint({LIST_HELP!r} if '--help' in sys.argv else {DEVICE!r})\n")
        self.iproxy.write_text(f"""#!{sys.executable}
import os, pathlib, select, signal, socket, sys, threading, time
if '--help' in sys.argv:
    print({HELP!r})
    sys.exit(0)
if {exit_early!r}:
    print({DEVICE!r})
    sys.exit(3)
assert sys.argv[1:6] == ['--local', '--source', '127.0.0.1', '--udid', {DEVICE!r}]
assert sys.argv[6].split(':')[1] == '8081'
def stopped(signum, frame):
    pathlib.Path({str(self.marker)!r}).write_text('TERM')
    sys.exit(0)
signal.signal(signal.SIGTERM, stopped)
if {exit_later!r}:
    threading.Timer(.15, lambda: os._exit(4)).start()
listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(('127.0.0.1', int(sys.argv[6].split(':')[0])))
listener.listen(8)
print({DEVICE!r}, flush=True)  # deliberately noisy fake device output must stay suppressed
def forward(client):
    try:
        with client, socket.create_connection(('127.0.0.1', {backend_port}), timeout=1) as upstream:
            while True:
                readable, _, _ = select.select([client, upstream], [], [], 2)
                if not readable:
                    return
                for source in readable:
                    block = source.recv(16384)
                    if not block:
                        return
                    (upstream if source is client else client).sendall(block)
    except OSError:
        pass
while True:
    client, _ = listener.accept()
    threading.Thread(target=forward, args=(client,), daemon=True).start()
""")
        self.iproxy.chmod(0o755)
        self.listing.chmod(0o755)


class Backend:
    def __init__(self, packet=HEALTH, status=200, trickle=False):
        self.paths = []
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.paths.append(self.path)
                try:
                    if trickle:
                        for byte in b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}":
                            self.connection.sendall(bytes([byte]))
                            time.sleep(.025)
                        return
                    body = json.dumps(packet).encode()
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Location", "http://private-account.invalid/control")
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
    @property
    def port(self): return self.server.server_port
    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(1)


class PolicyTests(unittest.TestCase):
    def test_modern_usb_only_source_and_port_pair_capabilities_required(self):
        require_modern_iproxy(HELP)
        for missing in ("--local", "USB", "--source", "--udid", "LOCAL_PORT:DEVICE_PORT"):
            with self.subTest(missing=missing), self.assertRaises(BridgeError):
                require_modern_iproxy(HELP.replace(missing, "legacy"))
        with self.assertRaisesRegex(BridgeError, "legacy"):
            require_modern_iproxy("Usage: iproxy LOCAL_PORT DEVICE_PORT [UDID]")

    def test_device_selection_rejects_missing_ambiguous_network_and_unknown_without_logging_ids(self):
        self.assertEqual(select_usb_device(DEVICE), DEVICE)
        self.assertEqual(select_usb_device(DEVICE + "\n" + OTHER_DEVICE, OTHER_DEVICE), OTHER_DEVICE)
        for text, requested in (("", None), (DEVICE + "\n" + OTHER_DEVICE, None),
                                (DEVICE + " (Network)", None), (DEVICE, OTHER_DEVICE),
                                (DEVICE + "\n" + DEVICE, None)):
            with self.assertRaises(BridgeError) as raised:
                select_usb_device(text, requested)
            self.assertNotIn(DEVICE, str(raised.exception))
            self.assertNotIn(OTHER_DEVICE, str(raised.exception))

    def test_existing_listener_and_invalid_ports_are_refused(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with self.assertRaises(BridgeError): preflight_port(listener.getsockname()[1])
        for port in (True, 80, 65536):
            with self.assertRaises(BridgeError): preflight_port(port)

    def test_no_remote_usbmux_override_or_missing_tool_auto_install(self):
        with patch.dict(os.environ, USBMUXD_SOCKET_ADDRESS="remote-account:27015"):
            with self.assertRaisesRegex(BridgeError, "Unset"):
                PhoneUSB(available_port()).start()
        with patch.dict(os.environ, {}, clear=True), patch("tools.phone_usb.shutil.which", return_value=None), \
                patch("tools.phone_usb.subprocess.Popen") as spawn:
            with self.assertRaisesRegex(BridgeError, "Install"):
                PhoneUSB(available_port()).start()
            spawn.assert_not_called()

    def test_missing_or_ambiguous_usb_enumeration_never_starts_iproxy(self):
        for output in ("", DEVICE + "\n" + OTHER_DEVICE):
            with patch.dict(os.environ, {}, clear=True), \
                    patch("tools.phone_usb.tool_output", side_effect=[HELP, LIST_HELP, output]), \
                    patch("tools.phone_usb.subprocess.Popen") as spawn:
                with self.assertRaises(BridgeError):
                    PhoneUSB(available_port(), iproxy="iproxy", idevice_id="idevice_id").start()
                spawn.assert_not_called()

    def test_sanitized_backend_summary_discards_accounts_tokens_and_unknown_free_text(self):
        summary = health_summary(HEALTH)
        self.assertEqual(summary["detector_backend"], "pose-tensorrt")
        self.assertNotIn("secret", json.dumps(summary))
        self.assertNotIn("hidden-account", json.dumps(summary))
        summary = health_summary(dict(HEALTH, camera_source="private-phone-name", detector_backend="/private/model/path"))
        self.assertEqual((summary["camera_source"], summary["detector_backend"]), ("OTHER", "OTHER"))
        with self.assertRaises(BridgeError): health_summary(dict(camera=True))

    def test_close_escalates_for_stubborn_child(self):
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired("suppressed", 2), 0]
        proxy = PhoneUSB()
        proxy.process = child
        proxy.close()
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertIsNone(proxy.process)

    def test_cli_unsupported_platform_and_invalid_timeout_are_sanitized(self):
        output = StringIO()
        with patch("tools.phone_usb.platform.system", return_value="Linux"), redirect_stderr(output):
            self.assertEqual(main(["--device", DEVICE]), 1)
        self.assertNotIn(DEVICE, output.getvalue())
        for timeout in (float("nan"), -1, 31):
            with self.assertRaises(BridgeError): PhoneUSB(timeout=timeout)


class LoopbackLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                         if key != "USBMUXD_SOCKET_ADDRESS"}, clear=True)
        self.env.start()
        self.directory = tempfile.TemporaryDirectory(prefix="phone usb fixture ")
        self.backend = Backend()
    def tearDown(self):
        self.backend.close()
        self.directory.cleanup()
        self.env.stop()
    def proxy(self, **changes):
        fixture = FixturePrograms(self.directory.name, self.backend.port, **changes)
        proxy = PhoneUSB(available_port(), timeout=.5, iproxy=str(fixture.iproxy), idevice_id=str(fixture.listing))
        return fixture, proxy

    def test_actual_subprocess_forwards_only_health_and_terminates_cleanly(self):
        fixture, proxy = self.proxy()
        process = None
        try:
            proxy.start()
            process = proxy.process
            summary = proxy.check_health()
            self.assertEqual(summary["camera_source"], "PHONE RGB")
            self.assertEqual(summary["detector_backend"], "pose-tensorrt")
            self.assertEqual(self.backend.paths, ["/health"])
            self.assertTrue(proxy.process.args[1:6] == ["--local", "--source", "127.0.0.1", "--udid", DEVICE])
        finally:
            proxy.close()
        self.assertIsNotNone(process.poll())
        self.assertEqual(fixture.marker.read_text(), "TERM")
        preflight_port(proxy.local_port)

    def test_child_startup_failure_and_exit_are_detected_without_output(self):
        fixture, proxy = self.proxy(exit_early=True)
        with self.assertRaises(BridgeError) as raised:
            proxy.start()
        self.assertNotIn(DEVICE, str(raised.exception))
        self.assertIsNone(proxy.process)
        fixture, proxy = self.proxy(exit_later=True)
        try:
            proxy.start()
            time.sleep(.2)
            with self.assertRaises(BridgeError): proxy.ensure_running()
        finally:
            proxy.close()

    def test_cli_check_prints_sanitized_evidence_then_stops_proxy(self):
        fixture, proxy = self.proxy()
        output = StringIO()
        with patch("tools.phone_usb.PhoneUSB", return_value=proxy), \
                patch("tools.phone_usb.platform.system", return_value="Darwin"), redirect_stdout(output):
            self.assertEqual(main(["--check", "--local-port", str(proxy.local_port)]), 0)
        self.assertIsNone(proxy.process)
        self.assertTrue(fixture.marker.is_file())
        text = output.getvalue()
        self.assertNotIn(DEVICE, text)
        self.assertNotIn("secret", text)
        self.assertIn("--quest", text)
        self.assertIn('"check_stops_proxy": true', text)
        self.assertTrue(json.loads(text)["health_forwarding_observed"])

    def test_normal_cli_monitors_child_exit_and_reports_it_safely(self):
        fixture, proxy = self.proxy(exit_later=True)
        output, error = StringIO(), StringIO()
        with patch("tools.phone_usb.PhoneUSB", return_value=proxy), \
                patch("tools.phone_usb.platform.system", return_value="Darwin"), \
                redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(main(["--local-port", str(proxy.local_port)]), 1)
        self.assertIn("iproxy exited", error.getvalue())
        self.assertNotIn(DEVICE, output.getvalue() + error.getvalue())
        self.assertIsNone(proxy.process)

    @unittest.skipUnless(sys.platform == "darwin", "real CLI is macOS-only")
    def test_helper_sigterm_stops_actual_fixture_subprocess(self):
        fixture, proxy = self.proxy()
        environment = dict(os.environ, PATH=self.directory.name + os.pathsep + os.environ.get("PATH", ""))
        child = subprocess.Popen([sys.executable, "-m", "tools.phone_usb", "--local-port", str(proxy.local_port)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        try:
            self.assertEqual(child.stdout.readline().strip(), "{")
            child.terminate()
            remaining, error = child.communicate(timeout=4)
            self.assertEqual(child.returncode, 0)
            self.assertNotIn(DEVICE, remaining + error)
            self.assertEqual(fixture.marker.read_text(), "TERM")
            preflight_port(proxy.local_port)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)

    def test_redirect_or_oversize_health_is_refused_without_following_remote_locations(self):
        for status, packet in ((302, HEALTH), (200, dict(HEALTH, padding="x" * 70000))):
            self.backend.close()
            self.backend = Backend(packet, status)
            fixture, proxy = self.proxy()
            try:
                proxy.start()
                with self.assertRaises(BridgeError): proxy.check_health()
                self.assertEqual(self.backend.paths, ["/health"])
            finally:
                proxy.close()

    def test_trickled_headers_obey_complete_probe_deadline(self):
        self.backend.close()
        self.backend = Backend(trickle=True)
        fixture, proxy = self.proxy()
        try:
            proxy.start()
            proxy.timeout = .12
            started = time.monotonic()
            with self.assertRaises(BridgeError): proxy.check_health()
            self.assertLess(time.monotonic() - started, .5)
            self.assertEqual(self.backend.paths, ["/health"])
        finally:
            proxy.close()


if __name__ == "__main__":
    unittest.main()
