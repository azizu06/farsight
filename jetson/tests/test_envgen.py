import unittest
from pathlib import Path

from jetson import envgen, launch

EXAMPLE = Path(envgen.__file__).with_name("farsight.env.example")


def parse(text):
    return dict(line.split("=", 1) for line in text.splitlines() if line and not line.startswith("#") and "=" in line)


def render(present=(), **kwargs):
    present = set(present)
    return parse(envgen.render(EXAMPLE.read_text(), repo="/home/jet/farsight", exists=lambda p: p in present, **kwargs))


MODELS = "/home/jet/farsight/models"


class RenderTests(unittest.TestCase):
    def test_pose_engine_preferred_over_onnx(self):
        env = render(present=(f"{MODELS}/yolo11n-pose.engine", f"{MODELS}/yolo11n-pose.onnx"))
        self.assertEqual(env["FARSIGHT_DETECTOR"], "pose")
        self.assertEqual(env["FARSIGHT_DETECTOR_MODEL"], f"{MODELS}/yolo11n-pose.engine")

    def test_onnx_fallback_when_engine_missing(self):
        env = render(present=(f"{MODELS}/yolo11n-pose.onnx",))
        self.assertEqual(env["FARSIGHT_DETECTOR_MODEL"], f"{MODELS}/yolo11n-pose.onnx")

    def test_no_model_falls_back_to_hog_so_the_unit_still_starts(self):
        env = render()
        self.assertEqual(env["FARSIGHT_DETECTOR"], "hog")
        self.assertEqual(env["FARSIGHT_DETECTOR_MODEL"], "")

    def test_optional_models_only_set_when_present(self):
        env = render()
        for key in ("FARSIGHT_FACE_MODEL", "FARSIGHT_PIPER_MODEL", "FARSIGHT_PIPER_MODEL_ES",
                    "FARSIGHT_SSL_CERTFILE", "FARSIGHT_SSL_KEYFILE", "FARSIGHT_ASSISTANT_URL", "FARSIGHT_WHISPER_URL"):
            self.assertEqual(env[key], "", key)
        env = render(present=(f"{MODELS}/version-RFB-320.onnx", f"{MODELS}/piper/en_US-lessac-medium.onnx",
                              f"{MODELS}/piper/es_MX-claude-high.onnx", "/etc/farsight/tls/cert.pem", "/etc/farsight/tls/key.pem"))
        self.assertEqual(env["FARSIGHT_FACE_MODEL"], f"{MODELS}/version-RFB-320.onnx")
        self.assertEqual(env["FARSIGHT_PIPER_MODEL"], f"{MODELS}/piper/en_US-lessac-medium.onnx")
        self.assertEqual(env["FARSIGHT_PIPER_MODEL_ES"], f"{MODELS}/piper/es_MX-claude-high.onnx")
        self.assertEqual(env["FARSIGHT_SSL_CERTFILE"], "/etc/farsight/tls/cert.pem")

    def test_depth_model_prefers_engine(self):
        base = f"{MODELS}/depth_anything_v2_metric_indoor_small_308x406"
        self.assertEqual(render(present=(f"{base}.onnx", f"{base}.engine"))["FARSIGHT_DEPTH_MODEL"], f"{base}.engine")
        self.assertEqual(render(present=(f"{base}.onnx",))["FARSIGHT_DEPTH_MODEL"], f"{base}.onnx")
        self.assertEqual(render()["FARSIGHT_DEPTH_MODEL"], "")

    def test_tls_needs_both_files(self):
        env = render(present=("/etc/farsight/tls/cert.pem",))
        self.assertEqual((env["FARSIGHT_SSL_CERTFILE"], env["FARSIGHT_SSL_KEYFILE"]), ("", ""))

    def test_assistant_urls_follow_the_installed_units(self):
        env = render(assistant=True, whisper=True)
        self.assertEqual(env["FARSIGHT_ASSISTANT_URL"], "http://127.0.0.1:8092")
        self.assertEqual(env["FARSIGHT_WHISPER_URL"], "http://127.0.0.1:8091")

    def test_serial_port_picks_the_device_that_exists(self):
        self.assertEqual(render(present=("/dev/ttyUSB0",))["FARSIGHT_SERIAL_PORT"], "/dev/ttyUSB0")
        self.assertEqual(render(present=("/dev/ttyACM1", "/dev/ttyUSB0"))["FARSIGHT_SERIAL_PORT"], "/dev/ttyACM1")
        self.assertEqual(render()["FARSIGHT_SERIAL_PORT"], "/dev/ttyACM0")

    def test_comments_and_unknown_keys_survive(self):
        text = envgen.render("# keep me\nFARSIGHT_PORT=8443\nOTHER=1\n", repo="/r", exists=lambda p: False)
        self.assertIn("# keep me", text)
        self.assertIn("FARSIGHT_PORT=8443", text)
        self.assertIn("OTHER=1", text)

    def test_rendered_example_passes_launch_validation(self):
        env = render(present=(f"{MODELS}/yolo11n-pose.engine",), assistant=True, whisper=True)
        launch.build_args(env)

    def test_example_declares_every_variable_launch_understands(self):
        keys = set(parse(EXAMPLE.read_text()))
        known = {name for name, _ in launch.VALUE_FLAGS} | {name for name, _ in launch.BOOL_FLAGS} | {
            "FARSIGHT_HOST", "FARSIGHT_PORT", "FARSIGHT_WAIT_SECONDS"}
        self.assertEqual(sorted(known - keys), [])


if __name__ == "__main__":
    unittest.main()
