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

from tools.phone_usb import BridgeError, PhoneUSB, main, require_modern_iproxy, select_usb_device, preflight_port, health_summary, geometry_summary


DEVICE = "a" * 40
OTHER_DEVICE = "b" * 40
HELP = """Usage: iproxy [OPTIONS] LOCAL_PORT:DEVICE_PORT [LOCAL_PORT2:DEVICE_PORT2 ...]
  --local     USB-only device connection
  --source ADDR listening address
  --udid UDID explicit target
"""
LIST_HELP = "--list list USB devices\n--network network devices\n"
HEALTH = dict(camera=True, camera_drive_ready=True, camera_source="PHONE RGB", rover=False,
              detector="pose", detector_backend="pose-tensorrt", video=dict(ready=True),
              control=dict(simulated=False, session_id="secret-controller", permit="secret-permit"),
              replay=False, private_account="hidden-account")
GEOMETRY = dict(geometry=True, protocol=1, source="IPHONE ARKIT MESH", ready=True,
                session_id="private-session-id", map_epoch=1, anchors=12, vertices=1500,
                triangles=2200, dropped=2, private_account="hidden-account")


def available_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FixturePrograms:
    def __init__(self, directory, backend_port, exit_early=False, exit_later=False,
                 secondary_backend_port=None, partial=False, fail_second=False,
                 geometry_backend_port=None, missing_geometry=False, fail_geometry=False):
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
specs = sys.argv[6:]
assert [int(spec.split(':')[1]) for spec in specs] in ([8081], [8081, 8082], [8081, 8083], [8081, 8082, 8083])
if {partial!r}:
    specs = specs[:1]
if {missing_geometry!r}:
    specs = [spec for spec in specs if spec.split(':')[1] != '8083']
def stopped(signum, frame):
    pathlib.Path({str(self.marker)!r}).write_text('TERM')
    sys.exit(0)
signal.signal(signal.SIGTERM, stopped)
if {exit_later!r}:
    threading.Timer(.15, lambda: os._exit(4)).start()
listeners = {{}}
for index, spec in enumerate(specs):
    phone_port = int(spec.split(':')[1])
    if (index == 1 and {fail_second!r}) or (phone_port == 8083 and {fail_geometry!r}):
        sys.exit(4)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', int(spec.split(':')[0])))
    listener.listen(8)
    listeners[listener] = {{8081: {backend_port}, 8082: {secondary_backend_port or backend_port}, 8083: {geometry_backend_port or backend_port}}}[phone_port]
print({DEVICE!r}, flush=True)  # deliberately noisy fake device output must stay suppressed
def forward(client, upstream_port):
    try:
        with client, socket.create_connection(('127.0.0.1', upstream_port), timeout=1) as upstream:
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
    ready, _, _ = select.select(list(listeners), [], [])
    for listener in ready:
        client, _ = listener.accept()
        threading.Thread(target=forward, args=(client, listeners[listener]), daemon=True).start()
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
        with self.assertRaisesRegex(BridgeError, "multiple port pairs"):
            require_modern_iproxy(HELP.replace("[LOCAL_PORT2:DEVICE_PORT2 ...]", ""), dual_route=True)

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

    def test_dual_ports_must_be_distinct_and_preflight_checks_secondary_before_spawning(self):
        port = available_port()
        with self.assertRaisesRegex(BridgeError, "distinct"):
            PhoneUSB(port, secondary_local_port=port).start()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with patch("tools.phone_usb.subprocess.Popen") as spawn:
                with self.assertRaisesRegex(BridgeError, "already in use"):
                    PhoneUSB(available_port(), secondary_local_port=listener.getsockname()[1]).start()
                spawn.assert_not_called()

    def test_secondary_cli_override_requires_explicit_opt_in(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(["--secondary-local-port", "18444"])
        self.assertEqual(raised.exception.code, 2)

    def test_default_single_route_and_opt_in_default_secondary_port_are_explicit(self):
        for options, secondary in ((["--check"], None), (["--dual-route", "--check"], 18444)):
            proxy = Mock(PHONE_PORT=8081, SECONDARY_PHONE_PORT=8082, GEOMETRY_PHONE_PORT=8083)
            proxy.start.return_value = proxy
            proxy.check_health.return_value = health_summary(HEALTH)
            output = StringIO()
            with patch("tools.phone_usb.PhoneUSB", return_value=proxy) as factory, \
                    patch("tools.phone_usb.platform.system", return_value="Darwin"), redirect_stdout(output):
                self.assertEqual(main(options), 0)
            factory.assert_called_once_with(18443, None, 5, secondary_local_port=secondary, geometry_local_port=None)
            report = json.loads(output.getvalue())
            self.assertEqual(report["secondary_local_port"], secondary)
            self.assertEqual(len(report["routes"]), 1 if secondary is None else 2)
            self.assertEqual("--bonded-upstream" in report["next_step"], secondary is not None)
            proxy.close.assert_called_once()

    def test_geometry_defaults_and_explicit_opt_in(self):
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            main(["--geometry-local-port", "18445"])
        self.assertEqual(raised.exception.code, 2)
        for options, secondary in (([], None), (["--dual-route"], 18444)):
            proxy = Mock(PHONE_PORT=8081, SECONDARY_PHONE_PORT=8082, GEOMETRY_PHONE_PORT=8083)
            proxy.start.return_value = proxy
            proxy.check_health.return_value = health_summary(HEALTH)
            proxy.check_geometry.return_value = geometry_summary(GEOMETRY)
            output = StringIO()
            with patch("tools.phone_usb.PhoneUSB", return_value=proxy) as factory, \
                    patch("tools.phone_usb.platform.system", return_value="Darwin"), redirect_stdout(output):
                self.assertEqual(main(options + ["--geometry", "--check"]), 0)
            factory.assert_called_once_with(18443, None, 5, secondary_local_port=secondary, geometry_local_port=18445)
            report = json.loads(output.getvalue())
            self.assertEqual(report["geometry_lane"]["local_port"], 18445)
            self.assertTrue(report["geometry_lane"]["status_forwarding_observed"])
            self.assertNotIn("geometry", report["next_step"])
            proxy.close.assert_called_once()

    def test_geometry_port_conflicts_are_preflighted_before_any_spawn(self):
        primary, other = available_port(), available_port()
        for secondary, geometry in ((None, primary), (other, other)):
            with patch("tools.phone_usb.subprocess.Popen") as spawn:
                with self.assertRaisesRegex(BridgeError, "distinct"):
                    PhoneUSB(primary, secondary_local_port=secondary, geometry_local_port=geometry).start()
                spawn.assert_not_called()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with patch("tools.phone_usb.subprocess.Popen") as spawn:
                with self.assertRaisesRegex(BridgeError, "already in use"):
                    PhoneUSB(primary, geometry_local_port=listener.getsockname()[1]).start()
                spawn.assert_not_called()

    def test_geometry_contract_rejects_wrong_types_and_sanitizes_private_fields(self):
        summary = geometry_summary(GEOMETRY)
        self.assertEqual(summary, dict(protocol=1, ready=True, anchors=12, vertices=1500, triangles=2200, dropped=2))
        self.assertFalse(geometry_summary(dict(GEOMETRY, ready=False))["ready"])
        for changed in (dict(geometry=1), dict(protocol=True), dict(protocol=2), dict(ready=1),
                        dict(anchors=True), dict(vertices=-1), dict(triangles=1.5), dict(dropped=2**53)):
            with self.subTest(changed=changed), self.assertRaises(BridgeError):
                geometry_summary(dict(GEOMETRY, **changed))
        with self.assertRaises(BridgeError): geometry_summary(HEALTH)
        with self.assertRaises(BridgeError): PhoneUSB().check_geometry()


class LoopbackLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {key: value for key, value in os.environ.items()
                                         if key != "USBMUXD_SOCKET_ADDRESS"}, clear=True)
        self.env.start()
        self.directory = tempfile.TemporaryDirectory(prefix="phone usb fixture ")
        self.backend = Backend()
        self.secondary_backend = None
        self.geometry_backend = None
    def tearDown(self):
        self.backend.close()
        if self.secondary_backend is not None:
            self.secondary_backend.close()
        if self.geometry_backend is not None:
            self.geometry_backend.close()
        self.directory.cleanup()
        self.env.stop()
    def proxy(self, **changes):
        fixture = FixturePrograms(self.directory.name, self.backend.port, **changes)
        proxy = PhoneUSB(available_port(), timeout=.5, iproxy=str(fixture.iproxy), idevice_id=str(fixture.listing))
        return fixture, proxy

    def dual_proxy(self, **changes):
        self.secondary_backend = Backend(dict(HEALTH, camera_source="USB CAMERA"))
        fixture = FixturePrograms(self.directory.name, self.backend.port,
                                  secondary_backend_port=self.secondary_backend.port, **changes)
        primary, secondary = available_port(), available_port()
        while secondary == primary:
            secondary = available_port()
        proxy = PhoneUSB(primary, timeout=.5, iproxy=str(fixture.iproxy),
                         idevice_id=str(fixture.listing), secondary_local_port=secondary)
        return fixture, proxy

    def geometry_proxy(self, dual=False, packet=GEOMETRY, status=200, **changes):
        self.geometry_backend = Backend(packet, status)
        if dual:
            self.secondary_backend = Backend(dict(HEALTH, camera_source="USB CAMERA"))
        fixture = FixturePrograms(self.directory.name, self.backend.port,
                                  secondary_backend_port=self.secondary_backend.port if dual else None,
                                  geometry_backend_port=self.geometry_backend.port, **changes)
        ports = set()
        while len(ports) < 3:
            ports.add(available_port())
        primary, secondary, geometry = ports
        proxy = PhoneUSB(primary, timeout=.5, iproxy=str(fixture.iproxy), idevice_id=str(fixture.listing),
                         secondary_local_port=secondary if dual else None, geometry_local_port=geometry)
        return fixture, proxy

    def geometry_check(self, proxy, dual=False):
        output = StringIO()
        options = ["--geometry", "--check", "--local-port", str(proxy.local_port),
                   "--geometry-local-port", str(proxy.geometry_local_port)]
        if dual:
            options += ["--dual-route", "--secondary-local-port", str(proxy.secondary_local_port)]
        with patch("tools.phone_usb.PhoneUSB", return_value=proxy), \
                patch("tools.phone_usb.platform.system", return_value="Darwin"), redirect_stdout(output):
            code = main(options)
        return code, json.loads(output.getvalue()), output.getvalue()

    def test_geometry_endpoint_and_fixed_two_or_three_mappings_are_independent(self):
        for dual in (False, True):
            with self.subTest(dual=dual):
                fixture, proxy = self.geometry_proxy(dual=dual)
                try:
                    proxy.start()
                    args = [f"{proxy.local_port}:8081"]
                    if dual:
                        args += [f"{proxy.secondary_local_port}:8082"]
                    args += [f"{proxy.geometry_local_port}:8083"]
                    self.assertEqual(proxy.process.args[6:], args)
                    self.assertEqual(proxy.process.args.count("--udid"), 1)
                    self.assertNotIn("--network", proxy.process.args)
                    self.assertTrue(proxy.check_health()["camera"])
                    self.assertTrue(proxy.check_geometry()["ready"])
                    with self.assertRaises(BridgeError): proxy.check_health(proxy.geometry_local_port)
                    self.assertEqual(self.geometry_backend.paths, ["/geometry/status"])
                    self.assertEqual(self.backend.paths[-1:], ["/health"])
                finally:
                    proxy.close()
                preflight_port(proxy.local_port)
                preflight_port(proxy.geometry_local_port)
                self.geometry_backend.close()
                self.geometry_backend = None

    def test_geometry_check_reports_sanitized_status_and_preserves_control_check(self):
        fixture, proxy = self.geometry_proxy(dual=True)
        code, report, text = self.geometry_check(proxy, dual=True)
        self.assertEqual(code, 0)
        self.assertTrue(report["health_forwarding_observed"])
        self.assertEqual(len(report["routes"]), 2)
        self.assertEqual(report["geometry_lane"]["status"], geometry_summary(GEOMETRY))
        self.assertTrue(report["geometry_lane"]["status_forwarding_observed"])
        for secret in (DEVICE, "secret", "private-session-id", "hidden-account"):
            self.assertNotIn(secret, text)
        self.assertIsNone(proxy.process)
        self.assertTrue(fixture.marker.is_file())

    def test_geometry_not_ready_is_a_failed_check_with_successful_control_evidence(self):
        fixture, proxy = self.geometry_proxy(packet=dict(GEOMETRY, ready=False))
        code, report, _ = self.geometry_check(proxy)
        self.assertEqual(code, 1)
        self.assertTrue(report["health_forwarding_observed"])
        self.assertTrue(report["geometry_lane"]["status_forwarding_observed"])
        self.assertFalse(report["geometry_lane"]["status"]["ready"])
        self.assertIn("start LiDAR mesh capture", report["geometry_lane"]["error"])
        self.assertIsNone(proxy.process)

    def test_open_geometry_socket_is_not_proof_of_endpoint_and_control_error_does_not_skip_geometry(self):
        for packet, status in ((HEALTH, 200), (GEOMETRY, 404)):
            fixture, proxy = self.geometry_proxy(packet=packet, status=status)
            code, report, _ = self.geometry_check(proxy)
            self.assertEqual(code, 1)
            self.assertTrue(report["health_forwarding_observed"])
            self.assertFalse(report["geometry_lane"]["status_forwarding_observed"])
            self.assertEqual(self.geometry_backend.paths, ["/geometry/status"])
            self.geometry_backend.close()
            self.geometry_backend = None
        fixture, proxy = self.geometry_proxy()
        with patch.object(proxy, "check_health", side_effect=BridgeError("Control health unavailable")):
            code, report, _ = self.geometry_check(proxy)
        self.assertEqual(code, 1)
        self.assertFalse(report["health_forwarding_observed"])
        self.assertTrue(report["geometry_lane"]["status_forwarding_observed"])
        self.assertEqual(self.geometry_backend.paths, ["/geometry/status"])

    def test_geometry_partial_startup_failure_closes_every_owned_mapping(self):
        for changes in (dict(missing_geometry=True), dict(fail_geometry=True)):
            fixture, proxy = self.geometry_proxy(dual=True, **changes)
            proxy.timeout = .2
            with self.assertRaises(BridgeError): proxy.start()
            self.assertIsNone(proxy.process)
            for port in (proxy.local_port, proxy.secondary_local_port, proxy.geometry_local_port):
                preflight_port(port)
            self.geometry_backend.close()
            self.geometry_backend = None
            self.secondary_backend.close()
            self.secondary_backend = None

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

    def test_actual_multiport_process_checks_both_fixed_mappings_and_closes_both(self):
        fixture, proxy = self.dual_proxy()
        process = None
        try:
            proxy.start()
            process = proxy.process
            self.assertEqual(process.args[6:], [f"{proxy.local_port}:8081", f"{proxy.secondary_local_port}:8082"])
            self.assertEqual(process.args.count("--udid"), 1)
            self.assertNotIn("--network", process.args)
            self.assertEqual(proxy.check_health()["camera_source"], "PHONE RGB")
            self.assertEqual(proxy.check_health(proxy.secondary_local_port)["camera_source"], "USB CAMERA")
            self.assertEqual(self.backend.paths, ["/health"])
            self.assertEqual(self.secondary_backend.paths, ["/health"])
            with self.assertRaises(BridgeError): proxy.check_health(80)
        finally:
            proxy.close()
        self.assertIsNotNone(process.poll())
        self.assertEqual(fixture.marker.read_text(), "TERM")
        preflight_port(proxy.local_port)
        preflight_port(proxy.secondary_local_port)

    def test_dual_check_prints_both_sanitized_health_results_and_exact_next_step(self):
        fixture, proxy = self.dual_proxy()
        output = StringIO()
        with patch("tools.phone_usb.PhoneUSB", return_value=proxy), \
                patch("tools.phone_usb.platform.system", return_value="Darwin"), redirect_stdout(output):
            self.assertEqual(main(["--dual-route", "--check", "--local-port", str(proxy.local_port),
                                   "--secondary-local-port", str(proxy.secondary_local_port)]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual([item["phone_port"] for item in report["routes"]], [8081, 8082])
        self.assertEqual([item["health"]["camera_source"] for item in report["routes"]], ["PHONE RGB", "USB CAMERA"])
        self.assertTrue(report["dual_route"])
        self.assertTrue(report["health_forwarding_observed"])
        self.assertEqual(report["next_step"], f"python -m tools.demo --upstream http://127.0.0.1:{proxy.local_port} --bonded-upstream http://127.0.0.1:{proxy.secondary_local_port} --quest")
        self.assertNotIn(DEVICE, output.getvalue())
        self.assertIsNone(proxy.process)
        self.assertTrue(fixture.marker.is_file())

    def test_partial_listener_or_second_bind_failure_cleans_up_owned_process_and_ports(self):
        for options in (dict(partial=True), dict(fail_second=True)):
            if self.secondary_backend is not None:
                self.secondary_backend.close()
                self.secondary_backend = None
            fixture, proxy = self.dual_proxy(**options)
            proxy.timeout = .2
            with self.assertRaises(BridgeError): proxy.start()
            self.assertIsNone(proxy.process)
            preflight_port(proxy.local_port)
            preflight_port(proxy.secondary_local_port)

    def test_dual_check_attempts_secondary_after_primary_health_failure_and_stops_both(self):
        fixture, proxy = self.dual_proxy()
        output = StringIO()
        with patch("tools.phone_usb.PhoneUSB", return_value=proxy), \
                patch("tools.phone_usb.platform.system", return_value="Darwin"), \
                patch.object(proxy, "check_health", side_effect=[BridgeError("Primary health unavailable"), health_summary(HEALTH)]) as check, \
                redirect_stdout(output):
            self.assertEqual(main(["--dual-route", "--check", "--local-port", str(proxy.local_port),
                                   "--secondary-local-port", str(proxy.secondary_local_port)]), 1)
        self.assertEqual([call.args for call in check.call_args_list], [(proxy.local_port,), (proxy.secondary_local_port,)])
        report = json.loads(output.getvalue())
        self.assertIsNone(report["routes"][0]["health"])
        self.assertIsNotNone(report["routes"][1]["health"])
        self.assertFalse(report["health_forwarding_observed"])
        self.assertIsNone(proxy.process)
        self.assertTrue(fixture.marker.is_file())

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
