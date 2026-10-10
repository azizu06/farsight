"""Realistic macOS command fixtures; tests never change networking."""

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import qualify_route as route


HARDWARE = """
Hardware Port: Ethernet Adapter (en3)
Device: en3
Ethernet Address: aa:bb:cc:dd:ee:ff

Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 11:22:33:44:55:66

Hardware Port: iPhone USB
Device: en5
Ethernet Address: 22:33:44:55:66:77
"""
SERVICES = """An asterisk (*) denotes that a network service is disabled.
(1) Linux for Tegra
(Hardware Port: Linux for Tegra, Device: en11)

(2) Wi-Fi
(Hardware Port: Wi-Fi, Device: en0)

(3) iPhone USB
(Hardware Port: iPhone USB, Device: en5)

(4) private-account-vpn-name
(Hardware Port: com.vendor.vpnclient, Device: )
"""
DEFAULT = """   route to: default
destination: default
       mask: default
    gateway: 172.20.10.1
  interface: en5
      flags: <UP,GATEWAY,DONE,STATIC,PRCLONING,GLOBAL>
 recvpipe  sendpipe  ssthresh  rtt,msec    rttvar  hopcount      mtu     expire
       0         0         0         0         0         0      1500         0
"""
ADAPTERS = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
    inet 127.0.0.1 netmask 0xff000000
    inet6 ::1 prefixlen 128
stf0: flags=0<> mtu 1280
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    ether 11:22:33:44:55:66
    status: inactive
en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    ether 22:33:44:55:66:77
    inet 172.20.10.4 netmask 0xfffffff0 broadcast 172.20.10.15
    status: active
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
    inet6 fe80::1234%utun3 prefixlen 64 scopeid 0x14
"""
JETSON = """en11: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
    inet 192.168.55.100 netmask 0xffffff00 broadcast 192.168.55.255
    status: active
"""


def fixture():
    outputs = {key: dict(ok=True, stdout=value, error=None) for key, value in
               dict(hardware=HARDWARE, services=SERVICES, default_route=DEFAULT, interfaces=ADAPTERS).items()}
    wifi = dict(en0=dict(ok=True, stdout="Wi-Fi Power (en0): Off\n", error=None))
    return outputs, wifi


class RouteEvidenceTests(unittest.TestCase):
    def test_tether_default_wifi_off_and_no_direct_usb_pass_only_local_policy(self):
        outputs, wifi = fixture()
        report = route.assess(outputs, wifi)
        self.assertTrue(report["route_policy_passed"])
        self.assertEqual(report["failed_checks"], [])
        self.assertFalse(report["mutations_performed"])
        self.assertIn("Does not verify the phone uses cellular rather than Wi-Fi", report["limitations"])
        self.assertEqual(report["checks"]["default_ipv4_via_iphone_usb"]["interface"], "en5")

    def test_configured_iphone_service_without_active_interface_fails(self):
        outputs, wifi = fixture()
        outputs["interfaces"]["stdout"] = ADAPTERS.replace("en5:", "en9:")
        self.assertIn("iphone_usb_active_ipv4", route.assess(outputs, wifi)["failed_checks"])

    def test_wifi_on_or_unreadable_power_fails_even_with_tether_default(self):
        for text, ok in (("Wi-Fi Power (en0): On", True), ("unexpected response", True),
                         ("Wi-Fi Power (en0): Off", False), ("Wi-Fi Power (en1): Off", True)):
            outputs, wifi = fixture()
            wifi["en0"] = dict(stdout=text, ok=ok)
            self.assertIn("wifi_off", route.assess(outputs, wifi)["failed_checks"])

    def test_default_wifi_vpn_or_missing_gateway_is_refused(self):
        for text in (DEFAULT.replace("interface: en5", "interface: en0"),
                     DEFAULT.replace("interface: en5", "interface: utun3"),
                     DEFAULT.replace("172.20.10.1", "link#5"),
                     DEFAULT.replace("UP,GATEWAY", "UP"), DEFAULT + " interface: en5\n"):
            outputs, wifi = fixture()
            outputs["default_route"]["stdout"] = text
            self.assertIn("default_ipv4_via_iphone_usb", route.assess(outputs, wifi)["failed_checks"])

    def test_active_direct_usb_rejected_even_when_phone_is_default(self):
        outputs, wifi = fixture()
        outputs["interfaces"]["stdout"] += JETSON
        report = route.assess(outputs, wifi)
        self.assertTrue(report["checks"]["default_ipv4_via_iphone_usb"]["passed"])
        self.assertIn("no_active_direct_jetson_usb_subnet", report["failed_checks"])
        self.assertEqual(report["checks"]["no_active_direct_jetson_usb_subnet"]["interfaces"], ["en11"])

    def test_inactive_direct_usb_allowed_but_unknown_status_and_overlapping_route_refused(self):
        for extra, passed in ((JETSON.replace("status: active", "status: inactive"), True),
                              (JETSON.replace("    status: active\n", ""), False),
                              (JETSON.replace("192.168.55.100", "192.168.50.100")
                               .replace("0xffffff00", "0xffff0000"), False)):
            outputs, wifi = fixture()
            outputs["interfaces"]["stdout"] += extra
            self.assertEqual(route.assess(outputs, wifi)["route_policy_passed"], passed)

    def test_custom_phone_service_and_subnet_are_honored(self):
        outputs, wifi = fixture()
        outputs["services"]["stdout"] = SERVICES.replace("(3) iPhone USB", "(3) Operator Phone")
        outputs["interfaces"]["stdout"] += JETSON
        report = route.assess(outputs, wifi, phone_service="Operator Phone", jetson_subnet="192.168.56.0/24")
        self.assertTrue(report["route_policy_passed"])

    def test_active_tailscale_bsd_point_to_point_ipv4_does_not_false_reject_phone_route(self):
        outputs, wifi = fixture()
        outputs["interfaces"]["stdout"] += """utun7: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
    inet 100.82.190.3 --> 100.82.190.3 netmask 0xffffffff
    inet6 fe80::abcd%utun7 prefixlen 64 scopeid 0x18
"""
        report = route.assess(outputs, wifi)
        self.assertTrue(report["route_policy_passed"])
        self.assertNotIn("100.82.190.3", json.dumps(report))

    def test_disabled_duplicate_or_wrong_hardware_iphone_services_are_refused(self):
        for text in (SERVICES.replace("(3) iPhone USB", "(3) *iPhone USB"),
                     SERVICES.replace("(3) iPhone USB", "(*) iPhone USB"),
                     SERVICES + "\n(5) iPhone USB\n(Hardware Port: iPhone USB, Device: en7)\n",
                     SERVICES.replace("(Hardware Port: iPhone USB", "(Hardware Port: Ethernet")):
            outputs, wifi = fixture()
            outputs["services"]["stdout"] = text
            self.assertIn("iphone_usb_service", route.assess(outputs, wifi)["failed_checks"])

    def test_missing_wifi_hardware_or_ambiguous_devices_fail_closed(self):
        for text in (HARDWARE.replace("Hardware Port: Wi-Fi", "Hardware Port: Ethernet"),
                     HARDWARE + "\nHardware Port: Wi-Fi\nDevice: en8\n"):
            outputs, wifi = fixture()
            outputs["hardware"]["stdout"] = text
            self.assertIn("wifi_inventory", route.assess(outputs, wifi)["failed_checks"])

    def test_malformed_truncated_or_duplicate_interface_evidence_fails(self):
        for text in ("", ADAPTERS.replace("0xfffffff0", "not-a-mask"), ADAPTERS + JETSON + JETSON):
            outputs, wifi = fixture()
            outputs["interfaces"]["stdout"] = text
            self.assertIn("interface_parse", route.assess(outputs, wifi)["failed_checks"])

    def test_every_failed_read_query_fails_even_if_stdout_looks_valid(self):
        for key in fixture()[0]:
            outputs, wifi = fixture()
            outputs[key]["ok"], outputs[key]["error"] = False, "timeout"
            self.assertIn(key + "_query", route.assess(outputs, wifi)["failed_checks"])

    def test_report_contains_no_addresses_account_names_or_raw_command_output(self):
        outputs, wifi = fixture()
        rendered = json.dumps(route.assess(outputs, wifi))
        for private in ("aa:bb:cc:dd:ee:ff", "11:22:33:44:55:66", "private-account-vpn-name",
                        "172.20.10.1", "172.20.10.4", "fe80::1234", "Ethernet Address", "stdout"):
            self.assertNotIn(private, rendered)


class CollectorTests(unittest.TestCase):
    def test_collector_runs_only_fixed_read_queries_and_explicit_wifi_query(self):
        outputs, wifi = fixture()
        calls = []
        def runner(argv):
            calls.append(argv)
            key = {"-listallhardwareports": "hardware", "-listnetworkserviceorder": "services",
                   "-n": "default_route", "-a": "interfaces"}.get(argv[1])
            return outputs[key] if key else wifi[argv[2]]
        report = route.collect(runner=runner, system="Darwin")
        self.assertTrue(report["route_policy_passed"])
        self.assertEqual(sorted(calls), sorted([
            ["/usr/sbin/networksetup", "-listallhardwareports"],
            ["/usr/sbin/networksetup", "-listnetworkserviceorder"],
            ["/sbin/route", "-n", "get", "default"], ["/sbin/ifconfig", "-a"],
            ["/usr/sbin/networksetup", "-getairportpower", "en0"]]))

    def test_non_macos_is_unsupported_without_running_commands(self):
        def runner(argv): self.fail("unsupported platform must not run queries")
        report = route.collect(runner=runner, system="Linux")
        self.assertFalse(report["route_policy_passed"])
        self.assertEqual(report["failed_checks"], ["unsupported_platform"])

    def test_command_timeout_failure_and_oversize_outputs_are_sanitized(self):
        argv = ["/sbin/route", "-n", "get", "default"]
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired(argv, 3)):
            self.assertEqual(route.command(argv)["error"], "timeout")
        with patch.object(subprocess, "run", side_effect=OSError("private path details")):
            self.assertNotIn("private", json.dumps(route.command(argv)))
        with patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="x" * 65537)):
            self.assertEqual(route.command(argv), dict(ok=False, stdout="", error="output_too_large"))
        with patch.object(subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")) as run:
            self.assertEqual(route.command(argv)["error"], "command_failed")
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertEqual(run.call_args.args[0], argv)

    def test_cli_exit_status_reflects_policy_and_invalid_settings_are_rejected(self):
        for passed in (False, True):
            with patch.object(route, "collect", return_value=dict(route_policy_passed=passed)), redirect_stdout(StringIO()):
                self.assertEqual(route.main([]), 0 if passed else 1)
        for argv in (["--jetson-usb-subnet", "::/64"], ["--phone-service", ""]):
            with patch("sys.stderr", StringIO()), self.assertRaises(SystemExit) as raised:
                route.main(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_output_persists_failure_evidence_identical_to_printed_json(self):
        outputs, wifi = fixture()
        outputs["interfaces"]["stdout"] += JETSON
        report = route.assess(outputs, wifi)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "route.json"
            path.write_text("old evidence")
            printed = StringIO()
            with patch.object(route, "collect", return_value=report), redirect_stdout(printed):
                self.assertEqual(route.main(["--output", str(path)]), 1)
            self.assertEqual(json.loads(path.read_text()), report)
            self.assertEqual(path.read_text(), printed.getvalue())
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_output_write_failure_exits_nonzero_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "route.json"
            path.write_text("old evidence")
            with patch.object(route, "collect", return_value=dict(route_policy_passed=True)), \
                    patch.object(route.os, "replace", side_effect=OSError()), \
                    redirect_stdout(StringIO()), patch("sys.stderr", StringIO()), \
                    self.assertRaises(SystemExit) as raised:
                route.main(["--output", str(path)])
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(path.read_text(), "old evidence")
            self.assertEqual(list(Path(directory).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
