import unittest
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


class MainTests(unittest.TestCase):
    def test_missing_device_still_starts_degraded(self):
        with mock.patch.object(launch, "wait_for_devices", return_value=["/dev/ttyACM0"]), \
             mock.patch.object(launch.os, "execv") as execv:
            launch.main({"FARSIGHT_SERIAL_PORT": "/dev/ttyACM0"})
        argv = execv.call_args[0][1]
        self.assertEqual(argv[1:4], ["-m", "jetson.mapping.server", "--host"])
        self.assertIn("--serial-port", argv)


if __name__ == "__main__":
    unittest.main()
