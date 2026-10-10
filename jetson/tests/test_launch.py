import os
import tempfile
import unittest
from io import StringIO
from unittest import mock

from jetson import launch


class BuildArgsTests(unittest.TestCase):
    def test_defaults_are_motor_disabled_hog(self):
        self.assertEqual(launch.build_args({}), ["--host", "0.0.0.0", "--port", "8443"])

    def test_full_env_maps_to_server_flags(self):
        env = {
            "FARSIGHT_CAMERA": "/dev/video0", "FARSIGHT_SERIAL_PORT": "/dev/ttyACM0",
            "FARSIGHT_DETECTOR_MODEL": "/m/yolo.engine", "FARSIGHT_MISSION_DIR": "/var/lib/farsight",
            "FARSIGHT_SSL_CERTFILE": "/c.pem", "FARSIGHT_SSL_KEYFILE": "/k.pem",
            "FARSIGHT_PORT": "9000", "FARSIGHT_MAX_PWM": "120", "FARSIGHT_MIN_PWM": "40",
            "FARSIGHT_H264": "1", "FARSIGHT_DETECTOR": "none",
        }
        args = launch.build_args(env)
        for pair in (["--camera", "/dev/video0"], ["--serial-port", "/dev/ttyACM0"],
                     ["--detector-model", "/m/yolo.engine"], ["--mission-dir", "/var/lib/farsight"],
                     ["--ssl-certfile", "/c.pem"], ["--ssl-keyfile", "/k.pem"], ["--port", "9000"],
                     ["--max-pwm", "120"], ["--min-pwm", "40"], ["--detector", "none"]):
            i = args.index(pair[0])
            self.assertEqual(args[i:i + 2], pair)
        self.assertIn("--h264", args)

    def test_assistant_env_maps_to_server_flags(self):
        env = {"FARSIGHT_ASSISTANT_URL": "http://127.0.0.1:8092", "FARSIGHT_WHISPER_URL": "http://127.0.0.1:8091",
               "FARSIGHT_WHISPER_BIN": "whisper-cli", "FARSIGHT_WHISPER_MODEL": "/m/ggml-base.en.bin",
               "FARSIGHT_PIPER_BIN": "piper", "FARSIGHT_PIPER_MODEL": "/m/en_US-lessac-low.onnx",
               "FARSIGHT_TTS_DIR": "/var/lib/farsight/tts"}
        args = launch.build_args(env)
        for pair in (["--assistant-url", "http://127.0.0.1:8092"], ["--whisper-url", "http://127.0.0.1:8091"],
                     ["--whisper-bin", "whisper-cli"], ["--whisper-model", "/m/ggml-base.en.bin"],
                     ["--piper-bin", "piper"], ["--piper-model", "/m/en_US-lessac-low.onnx"],
                     ["--tts-dir", "/var/lib/farsight/tts"]):
            i = args.index(pair[0])
            self.assertEqual(args[i:i + 2], pair)

    def test_face_video_triage_and_simulation_map_to_server_flags(self):
        env = {"FARSIGHT_FACE_MODEL": "/m/version-RFB-320.onnx", "FARSIGHT_VIDEO_FPS": "15",
               "FARSIGHT_VIDEO_BITRATE": "800000", "FARSIGHT_TRIAGE_INTERVAL": "4.5",
               "FARSIGHT_PIPER_MODEL_ES": "/m/es.onnx", "FARSIGHT_SPEAKER_CMD": "aplay -q",
               "FARSIGHT_SIMULATE_ROVER": "yes"}
        args = launch.build_args(env)
        for pair in (["--face-model", "/m/version-RFB-320.onnx"], ["--video-fps", "15"],
                     ["--video-bitrate", "800000"], ["--triage-interval", "4.5"],
                     ["--piper-model-es", "/m/es.onnx"], ["--speaker-cmd", "aplay -q"]):
            i = args.index(pair[0])
            self.assertEqual(args[i:i + 2], pair)
        self.assertIn("--simulate-rover", args)

    def test_scan_depth_and_record_map_to_server_flags(self):
        env = {"FARSIGHT_DEPTH_MODEL": "/m/depth.engine", "FARSIGHT_SCAN_FOV": "78", "FARSIGHT_SCAN_TURN_DPS": "85.5",
               "FARSIGHT_SCAN_INTRINSICS": "500, 500 320 240", "FARSIGHT_SCAN_PAN": "1", "FARSIGHT_RECORD": "true"}
        args = launch.build_args(env)
        for pair in (["--depth-model", "/m/depth.engine"], ["--scan-fov", "78"], ["--scan-turn-dps", "85.5"]):
            i = args.index(pair[0])
            self.assertEqual(args[i:i + 2], pair)
        i = args.index("--scan-intrinsics")
        self.assertEqual(args[i:i + 5], ["--scan-intrinsics", "500", "500", "320", "240"])
        self.assertIn("--scan-pan", args)
        self.assertIn("--record", args)

    def test_bad_scan_intrinsics_rejected(self):
        for bad in ("1 2 3", "a b c d"):
            with self.assertRaises(ValueError):
                launch.build_args({"FARSIGHT_SCAN_INTRINSICS": bad})

    def test_simulate_rover_conflicts_with_serial_port(self):
        with self.assertRaises(ValueError):
            launch.build_args({"FARSIGHT_SIMULATE_ROVER": "1", "FARSIGHT_SERIAL_PORT": "/dev/ttyACM0"})

    def test_bad_float_rejected(self):
        with self.assertRaises(ValueError):
            launch.build_args({"FARSIGHT_TRIAGE_INTERVAL": "soon"})

    def test_bulk_profile_accepts_disabled_or_measured_rate_and_rejects_invalid(self):
        for rate in ("0", "1.4"):
            args = launch.build_args({"FARSIGHT_BULK_LIMIT_MBPS": rate})
            index = args.index("--bulk-limit-mbps")
            self.assertEqual(args[index:index + 2], ["--bulk-limit-mbps", rate])
        for rate in ("nan", "inf", "-1", "slow"):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                launch.build_args({"FARSIGHT_BULK_LIMIT_MBPS": rate})

    def test_blank_values_ignored_and_false_flag_off(self):
        args = launch.build_args({"FARSIGHT_CAMERA": "  ", "FARSIGHT_H264": "0"})
        self.assertNotIn("--camera", args)
        self.assertNotIn("--h264", args)

    def test_bad_number_rejected(self):
        with self.assertRaises(ValueError):
            launch.build_args({"FARSIGHT_PORT": "abc"})

    def test_cert_without_key_rejected(self):
        with self.assertRaises(ValueError):
            launch.build_args({"FARSIGHT_SSL_CERTFILE": "/c.pem"})


class WaitTests(unittest.TestCase):
    def test_returns_missing_after_timeout(self):
        clock = iter(range(0, 100))
        missing = launch.wait_for_devices(["/dev/nope"], timeout=3, exists=lambda p: False,
                                          now=lambda: next(clock), sleep=lambda s: None)
        self.assertEqual(missing, ["/dev/nope"])

    def test_returns_empty_once_present(self):
        seen = iter([False, False, True])
        clock = iter(range(0, 100))
        missing = launch.wait_for_devices(["/dev/video0"], timeout=30, exists=lambda p: next(seen),
                                          now=lambda: next(clock), sleep=lambda s: None)
        self.assertEqual(missing, [])

    def test_non_path_camera_index_not_waited(self):
        self.assertEqual(launch.device_paths({"FARSIGHT_CAMERA": "0"}), [])
        self.assertEqual(launch.device_paths({"FARSIGHT_CAMERA": "/dev/video0",
                                              "FARSIGHT_SERIAL_PORT": "/dev/ttyACM0"}),
                         ["/dev/video0", "/dev/ttyACM0"])

    def test_rig_without_sensors_maps_proximity_and_pan_mount(self):
        args = launch.build_args({"FARSIGHT_PROXIMITY_STOP": "off", "FARSIGHT_PAN_MOUNT": "none",
                                  "FARSIGHT_SERIAL_PORT": "/dev/ttyUSB0"})
        args += launch.build_args({"FARSIGHT_MAX_PWM": "255", "FARSIGHT_MIN_PWM": "70", "FARSIGHT_AUTONOMY_MAX_PWM": "100",
                                   "FARSIGHT_DRIVE_PROFILE": "/etc/farsight/drive-profile.json",
                                   "FARSIGHT_ALLOW_CALIBRATION": "1"})
        self.assertIn("--allow-calibration", args)
        self.assertNotIn("--allow-calibration", launch.build_args({"FARSIGHT_ALLOW_CALIBRATION": ""}))
        for pair in (["--proximity-stop", "off"], ["--pan-mount", "none"], ["--serial-port", "/dev/ttyUSB0"],
                     ["--max-pwm", "255"], ["--min-pwm", "70"], ["--autonomy-max-pwm", "100"],
                     ["--drive-profile", "/etc/farsight/drive-profile.json"]):
            i = args.index(pair[0])
            self.assertEqual(args[i:i + 2], pair)


class ServerContractTests(unittest.TestCase):
    def test_every_launch_flag_is_accepted_by_the_canonical_server(self):
        import subprocess
        import sys
        help_text = subprocess.run([sys.executable, "-m", "jetson.server", "--help"], capture_output=True,
                                   text=True, check=True, cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__)))).stdout
        for flag in [f for _, f in launch.VALUE_FLAGS] + [f for _, f in launch.BOOL_FLAGS] + ["--host", "--port", "--scan-intrinsics"]:
            self.assertIn(flag, help_text, f"jetson.server has no {flag}; update jetson/launch.py")

    def test_physical_scan_pan_needs_a_pan_servo(self):
        from jetson.mapping.server import create_app
        with self.assertRaisesRegex(ValueError, "pan servo"):
            create_app(True, scan_pan=True, pan_mount="none")


class EnvFileTests(unittest.TestCase):
    def test_parses_systemd_style_lines(self):
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write('# comment\n\nFARSIGHT_PORT=9000\nFARSIGHT_CAMERA="/dev/video2"\nFARSIGHT_HOST=\n  \nBAD LINE\n')
        self.addCleanup(os.unlink, f.name)
        self.assertEqual(launch.read_env_file(f.name),
                         {"FARSIGHT_PORT": "9000", "FARSIGHT_CAMERA": "/dev/video2", "FARSIGHT_HOST": ""})


class PreflightTests(unittest.TestCase):
    def problems(self, env, present=()):
        return launch.preflight(env, exists=lambda p: p in present)

    def test_clean_env_has_no_problems(self):
        self.assertEqual(self.problems({"FARSIGHT_DETECTOR_MODEL": "/m/p.engine", "FARSIGHT_DETECTOR": "pose"},
                                       present=("/m/p.engine",)), [])

    def test_missing_files_are_named(self):
        env = {"FARSIGHT_DETECTOR": "pose", "FARSIGHT_DETECTOR_MODEL": "/m/p.engine",
               "FARSIGHT_FACE_MODEL": "/m/face.onnx", "FARSIGHT_PIPER_MODEL": "/m/en.onnx",
               "FARSIGHT_PIPER_MODEL_ES": "/m/es.onnx", "FARSIGHT_SSL_CERTFILE": "/c.pem", "FARSIGHT_SSL_KEYFILE": "/k.pem"}
        text = "\n".join(self.problems(env))
        for name in ("/m/p.engine", "/m/face.onnx", "/m/en.onnx", "/m/es.onnx", "/c.pem", "/k.pem"):
            self.assertIn(name, text)

    def test_pose_without_model_is_flagged(self):
        self.assertTrue(any("FARSIGHT_DETECTOR_MODEL" in p for p in self.problems({"FARSIGHT_DETECTOR": "pose"})))

    def test_missing_devices_are_flagged(self):
        env = {"FARSIGHT_CAMERA": "/dev/video0", "FARSIGHT_SERIAL_PORT": "/dev/ttyACM0"}
        self.assertEqual(len(self.problems(env)), 2)
        self.assertEqual(self.problems(env, present=("/dev/video0", "/dev/ttyACM0")), [])


class DryRunTests(unittest.TestCase):
    def test_check_prints_command_and_never_execs(self):
        out = StringIO()
        with mock.patch.object(launch.os, "execv") as execv:
            code = launch.main(["--check"], env={"FARSIGHT_PORT": "9000"}, out=out)
        execv.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("-m jetson.server --host 0.0.0.0 --port 9000", out.getvalue())

    def test_check_exits_nonzero_on_problems(self):
        out = StringIO()
        code = launch.main(["--check"], env={"FARSIGHT_CAMERA": "/dev/video99"}, out=out)
        self.assertEqual(code, 1)
        self.assertIn("/dev/video99", out.getvalue())

    def test_check_reports_bad_config(self):
        out = StringIO()
        self.assertEqual(launch.main(["--check"], env={"FARSIGHT_PORT": "abc"}, out=out), 1)
        self.assertIn("FARSIGHT_PORT", out.getvalue())


class MainTests(unittest.TestCase):
    def test_missing_device_still_starts_degraded(self):
        with mock.patch.object(launch, "wait_for_devices", return_value=["/dev/ttyACM0"]), \
             mock.patch.object(launch.os, "execv") as execv:
            launch.main([], env={"FARSIGHT_SERIAL_PORT": "/dev/ttyACM0"})
        argv = execv.call_args[0][1]
        self.assertEqual(argv[1:4], ["-m", "jetson.server", "--host"])
        self.assertIn("--serial-port", argv)


if __name__ == "__main__":
    unittest.main()
