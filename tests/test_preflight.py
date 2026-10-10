"""Read-only inventory policy: no device opens, service starts, or network mutations."""

import hashlib
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from tools import preflight


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def manifest(self, model="yolo.onnx", data=b"prepared-model", **extra):
        (self.root / model).write_bytes(data)
        fields = dict(model=model, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                      license="AGPL-3.0 or applicable Ultralytics license", source="local-fixture")
        fields.update(extra)
        (self.root / "yolo.manifest.json").write_text(json.dumps(fields))

    def test_prepared_model_size_hash_and_license_provenance(self):
        self.manifest()
        (self.root / "voice.onnx").write_bytes(b"unmanifested")
        report = preflight.model_inventory(self.root)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["manifests"][0]["detail"], "SHA-256 and size match")
        self.assertIn("AGPL", report["manifests"][0]["license"])
        self.assertIsNone(report["unmanifested_files"][0]["sha256"])
        self.assertFalse(report["gpu_models_qualified"])
        (self.root / "yolo.onnx").write_bytes(b"tampered-model")
        self.assertEqual(preflight.model_inventory(self.root)["status"], "fail")

    def test_missing_and_escaping_model_manifest_never_counts_as_verified(self):
        self.manifest()
        (self.root / "yolo.onnx").unlink()
        missing = preflight.model_inventory(self.root)
        self.assertIsNone(missing["manifests"][0]["sha256"])
        self.assertEqual(missing["status"], "fail")
        fields = dict(model="../secret.onnx", bytes=1, sha256="a" * 64)
        (self.root / "yolo.manifest.json").write_text(json.dumps(fields))
        with patch.object(preflight, "sha256_file", side_effect=AssertionError("must not read escaped path")):
            escaped = preflight.model_inventory(self.root)
        self.assertEqual(escaped["status"], "fail")
        absent = preflight.model_inventory(self.root / "absent")
        self.assertEqual(absent["status"], "unknown")
        self.assertEqual(absent["manifests"], [])

    def test_no_serial_port_is_opened_and_firmware_is_not_claimed(self):
        with patch("tools.preflight.glob.glob", return_value=["/dev/ttyACM0"]), patch("builtins.open", side_effect=AssertionError("serial open")):
            report = preflight.serial_inventory()
        self.assertEqual(report["candidate_ports"], ["/dev/ttyACM0"])
        self.assertFalse(report["serial_port_opened"])
        self.assertEqual(report["firmware_verification"], "not_checked")
        self.assertFalse(report["auto_flash"])

    def test_adb_authorized_unauthorized_offline_are_distinct(self):
        values = preflight.parse_adb_devices("quest-a\tdevice product:hollywood model:Quest_2\nquest-b\tunauthorized\nquest-c\toffline\n")
        self.assertEqual([item["authorized"] for item in values], [True, False, False])
        self.assertEqual([item["state"] for item in values], ["device", "unauthorized", "offline"])

    def test_adb_inventory_never_runs_a_command_that_autostarts_server(self):
        calls = []

        def command(args, **options):
            calls.append(args)
            return dict(state="ok", stdout="Android Debug Bridge version 1.0.41", stderr="", returncode=0)

        with patch("tools.preflight.shutil.which", return_value="/tool/adb"), patch("tools.preflight.command", side_effect=command), \
                patch("tools.preflight.adb_devices_read_only", return_value=([], "ok")):
            report = preflight.adb_inventory()
        self.assertEqual(calls, [["/tool/adb", "version"]])
        self.assertEqual(report["authorized_devices"], 0)
        self.assertFalse(report["quest_identity_verified"])
        self.assertEqual(report["status"], "warn")

    def test_adb_server_absence_is_unknown_instead_of_zero_devices(self):
        with patch("tools.preflight.socket.create_connection", side_effect=ConnectionRefusedError()):
            devices, state = preflight.adb_devices_read_only()
        self.assertIsNone(devices)
        self.assertEqual(state, "server_not_available")

    def test_local_adb_protocol_queries_existing_server_without_scan(self):
        class FakeSocket:
            def __init__(self):
                payload = b"quest\tdevice model:Quest_2\n"
                self.response = bytearray(b"OKAY" + f"{len(payload):04x}".encode() + payload)
                self.request = None
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, timeout): pass
            def sendall(self, data): self.request = data
            def recv(self, size):
                chunk = self.response[:size]
                del self.response[:size]
                return chunk
        server = FakeSocket()
        with patch("tools.preflight.socket.create_connection", return_value=server) as connect:
            devices, state = preflight.adb_devices_read_only()
        connect.assert_called_once_with(("127.0.0.1", 5037), timeout=.5)
        self.assertEqual(server.request, b"000ehost:devices-l")
        self.assertTrue(devices[0]["authorized"])
        self.assertEqual(state, "ok")

    def test_tailscale_missing_tool_and_authentication_states_are_truthful(self):
        with patch("tools.preflight.shutil.which", return_value=None):
            missing = preflight.tailscale_inventory()
        self.assertFalse(missing["cli_installed"])
        self.assertIsNone(missing["authenticated"])
        for backend, expected in (("Running", True), ("NeedsLogin", False), ("Starting", None)):
            with self.subTest(backend=backend), patch("tools.preflight.shutil.which", return_value="/bin/tailscale"), \
                    patch("tools.preflight.command", return_value=dict(state="ok", stdout=json.dumps({"BackendState": backend}))):
                report = preflight.tailscale_inventory()
            self.assertIs(report["authenticated"], expected)
            self.assertFalse(report["peers_tested"])

    def test_mac_gpu_checks_skipped_and_missing_gpu_is_never_count_zero(self):
        report = preflight.gpu_inventory(dict(jetson_detected=False), "operator")
        self.assertEqual(report["status"], "skipped")
        self.assertIsNone(report["gpu_device_count"])
        self.assertIsNone(report["cuda_version"])
        self.assertIsNone(report["tensorrt_version"])

    def test_jetson_runtime_version_is_usable_without_claiming_a_gpu_count(self):
        def command(args, **options):
            compile(args[2], "cuda-version-probe", "exec")
            return dict(state="ok", stdout=json.dumps({"version": "10.13.2", "cuda_runtime_version": "13.0"}))
        with patch("tools.preflight.command", side_effect=command), patch("tools.preflight.shutil.which", return_value=None), \
                patch("tools.preflight.Path.is_file", return_value=False):
            report = preflight.gpu_inventory(dict(jetson_detected=True), "rover")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["cuda_runtime_version"], "13.0")
        self.assertIsNone(report["cuda_compiler_version"])
        self.assertIsNone(report["gpu_device_count"])
        self.assertFalse(report["engine_execution_verified"])

    def test_ffmpeg_specific_encoder_is_checked_not_just_binary_presence(self):
        with patch("tools.preflight.shutil.which", return_value="/bin/ffmpeg"), \
                patch("tools.preflight.command", side_effect=[dict(state="ok", stdout="V....D libx264 H.264 encoder"),
                                                           dict(state="ok", stdout="ffmpeg version 8.0\n")]):
            report = preflight.ffmpeg_inventory()
        self.assertTrue(report["libx264_available"])
        self.assertFalse(report["stream_tested"])
        with patch("tools.preflight.shutil.which", return_value=None):
            absent = preflight.ffmpeg_inventory()
        self.assertIsNone(absent["libx264_available"])

    def test_dependency_version_bounds_use_the_declared_requirements(self):
        self.assertTrue(preflight.version_in_range("4.14.0", ">=4.10,<5"))
        self.assertFalse(preflight.version_in_range("5.0.0", ">=4.10,<5"))
        self.assertTrue(preflight.version_in_range("2.3.5", ">=1.26,<3"))
        self.assertFalse(preflight.version_in_range("3.0.0", ">=1.26,<3"))
        self.assertIsNone(preflight.version_in_range(None, ">=3.5,<4"))
        self.assertIsNone(preflight.version_in_range("1.0", "~=1.0"))

    def test_readonly_mac_network_service_parser(self):
        values = preflight.parse_network_services("(Hardware Port: iPhone USB, Device: en7)\n(Hardware Port: Wi-Fi, Device: en0)")
        self.assertEqual(values[0], dict(hardware_port="iPhone USB", interface="en7"))

    def test_timeout_unknown_not_fake_success_and_no_shell(self):
        with patch("tools.preflight.subprocess.run", side_effect=subprocess_timeout()) as run:
            report = preflight.command(["example", "--read-only"])
        self.assertEqual(report["state"], "timeout")
        self.assertIsNone(report["returncode"])
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertIs(run.call_args.kwargs["stdin"], preflight.subprocess.DEVNULL)


def subprocess_timeout():
    return preflight.subprocess.TimeoutExpired("example", 1)


if __name__ == "__main__":
    unittest.main()
