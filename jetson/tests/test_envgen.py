import unittest
import shlex
import subprocess
import tempfile
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

    def test_prepared_small_vision_whisper_and_root_piper_models_are_selected(self):
        present = [f"{MODELS}/{name}" for name in (*envgen.VISION_PAIRS[0], "ggml-tiny.en.bin", "en_US-lessac-low.onnx")]
        env = render(present=present)
        self.assertEqual(env["FARSIGHT_LLM_MODEL"], f"{MODELS}/SmolVLM-500M-Instruct-Q8_0.gguf")
        self.assertEqual(env["FARSIGHT_LLM_MMPROJ"], f"{MODELS}/mmproj-SmolVLM-500M-Instruct-Q8_0.gguf")
        self.assertEqual(env["FARSIGHT_WHISPER_MODEL"], f"{MODELS}/ggml-tiny.en.bin")
        self.assertEqual(env["FARSIGHT_PIPER_MODEL"], f"{MODELS}/en_US-lessac-low.onnx")

    def test_qwen_base_and_medium_voice_layout_remain_supported(self):
        present = {f"{MODELS}/{name}" for name in (*envgen.VISION_PAIRS[1], "ggml-base.en.bin", "piper/en_US-lessac-medium.onnx")}
        selection = envgen.model_selection("/home/jet/farsight", exists=present.__contains__)
        self.assertTrue(selection["llm_ready"])
        self.assertEqual(Path(selection["FARSIGHT_LLM_MODEL"]).name, envgen.VISION_PAIRS[1][0])
        self.assertEqual(Path(selection["FARSIGHT_WHISPER_MODEL"]).name, "ggml-base.en.bin")
        self.assertEqual(Path(selection["FARSIGHT_PIPER_MODEL"]).name, "en_US-lessac-medium.onnx")

    def test_partial_auto_pair_never_crosses_projectors_and_explicit_mixed_pair_is_not_ready(self):
        smol, qwen = envgen.SMOLVLM_PAIR, envgen.QWEN25_PAIR
        present = {f"{MODELS}/{name}" for name in (smol[0], *qwen)}
        selected = envgen.model_selection("/home/jet/farsight", exists=present.__contains__)
        self.assertEqual(Path(selected["FARSIGHT_LLM_MODEL"]).name, qwen[0])
        self.assertEqual(Path(selected["FARSIGHT_LLM_MMPROJ"]).name, qwen[1])
        selected = envgen.model_selection("/home/jet/farsight", exists=present.__contains__, overrides={
            "FARSIGHT_LLM_MODEL": f"{MODELS}/{smol[0]}", "FARSIGHT_LLM_MMPROJ": f"{MODELS}/{qwen[1]}"})
        self.assertFalse(selected["llm_ready"])
        self.assertTrue(selected["warnings"])
        present = {f"{MODELS}/{smol[0]}"}
        self.assertFalse(envgen.model_selection("/home/jet/farsight", exists=present.__contains__)["llm_ready"])

    def test_qwen3_pair_is_default_when_complete_and_never_borrows_another_projector(self):
        smol, qwen25, qwen3 = envgen.SMOLVLM_PAIR, envgen.QWEN25_PAIR, envgen.QWEN3_PAIR
        everything = {f"{MODELS}/{name}" for pair in (smol, qwen25, qwen3) for name in pair}
        chosen = envgen.model_selection("/home/jet/farsight", exists=everything.__contains__)
        self.assertEqual((Path(chosen["FARSIGHT_LLM_MODEL"]).name, Path(chosen["FARSIGHT_LLM_MMPROJ"]).name), qwen3)
        # Qwen3 model without its projector falls back to a complete older pair, never to a foreign projector.
        partial = everything - {f"{MODELS}/{qwen3[1]}"}
        chosen = envgen.model_selection("/home/jet/farsight", exists=partial.__contains__)
        self.assertEqual((Path(chosen["FARSIGHT_LLM_MODEL"]).name, Path(chosen["FARSIGHT_LLM_MMPROJ"]).name), smol)
        only_qwen3_model = {f"{MODELS}/{qwen3[0]}", f"{MODELS}/{qwen25[1]}", f"{MODELS}/{smol[1]}"}
        self.assertFalse(envgen.model_selection("/home/jet/farsight", exists=only_qwen3_model.__contains__)["llm_ready"])
        # Smol and Qwen2.5 remain selectable on their own and by explicit override.
        for pair in (smol, qwen25):
            present = {f"{MODELS}/{name}" for name in pair}
            chosen = envgen.model_selection("/home/jet/farsight", exists=present.__contains__)
            self.assertEqual((Path(chosen["FARSIGHT_LLM_MODEL"]).name, Path(chosen["FARSIGHT_LLM_MMPROJ"]).name), pair)
            chosen = envgen.model_selection("/home/jet/farsight", exists=everything.__contains__,
                                            overrides={"FARSIGHT_LLM_MODEL": f"{MODELS}/{pair[0]}"})
            self.assertEqual(Path(chosen["FARSIGHT_LLM_MMPROJ"]).name, pair[1])
        for wrong in (smol[1], qwen25[1]):
            chosen = envgen.model_selection("/home/jet/farsight", exists=everything.__contains__, overrides={
                "FARSIGHT_LLM_MODEL": f"{MODELS}/{qwen3[0]}", "FARSIGHT_LLM_MMPROJ": f"{MODELS}/{wrong}"})
            self.assertFalse(chosen["llm_ready"])
        chosen = envgen.model_selection("/home/jet/farsight", exists=everything.__contains__,
                                        overrides={"FARSIGHT_LLM_MMPROJ": f"{MODELS}/{qwen3[1]}"})
        self.assertEqual(Path(chosen["FARSIGHT_LLM_MODEL"]).name, qwen3[0])

    def test_whisper_prefers_base_then_tiny(self):
        both = {f"{MODELS}/ggml-tiny.en.bin", f"{MODELS}/ggml-base.en.bin"}
        self.assertEqual(Path(envgen.model_selection("/home/jet/farsight", exists=both.__contains__)["FARSIGHT_WHISPER_MODEL"]).name,
                         "ggml-base.en.bin")
        tiny = {f"{MODELS}/ggml-tiny.en.bin"}
        self.assertEqual(Path(envgen.model_selection("/home/jet/farsight", exists=tiny.__contains__)["FARSIGHT_WHISPER_MODEL"]).name,
                         "ggml-tiny.en.bin")

    def test_explicit_model_paths_override_sensible_automatic_choices(self):
        present = {f"{MODELS}/{name}" for pair in envgen.VISION_PAIRS for name in pair}
        present.update({"/other/ggml-medium.en.bin", "/other/voice.onnx"})
        overrides = dict(FARSIGHT_LLM_MODEL=f"{MODELS}/{envgen.VISION_PAIRS[1][0]}",
                         FARSIGHT_WHISPER_MODEL="/other/ggml-medium.en.bin", FARSIGHT_PIPER_MODEL="/other/voice.onnx")
        selected = envgen.model_selection("/home/jet/farsight", exists=present.__contains__, overrides=overrides)
        self.assertTrue(selected["llm_ready"])
        self.assertEqual(Path(selected["FARSIGHT_LLM_MMPROJ"]).name, envgen.VISION_PAIRS[1][1])
        self.assertEqual(selected["FARSIGHT_WHISPER_MODEL"], "/other/ggml-medium.en.bin")
        self.assertEqual(selected["FARSIGHT_PIPER_MODEL"], "/other/voice.onnx")
        explicit = {"FARSIGHT_LLM_MODEL": "/renamed/model space.gguf", "FARSIGHT_LLM_MMPROJ": "/renamed/projector space.gguf"}
        selected = envgen.model_selection("/home/jet/farsight", exists=lambda value: value in explicit.values(), overrides=explicit)
        self.assertTrue(selected["llm_ready"])
        self.assertEqual(selected["FARSIGHT_LLM_MODEL"], explicit["FARSIGHT_LLM_MODEL"])

    def test_phone_source_and_empty_serial_remove_usb_arguments_and_device_waits(self):
        env = render(camera_source="phone", serial_port="")
        self.assertEqual(env["FARSIGHT_CAMERA"], "")
        self.assertNotIn("--camera", launch.build_args(env))
        self.assertNotIn("--serial-port", launch.build_args(env))
        self.assertEqual(launch.device_paths(env), [])
        usb = render(camera_source="usb", serial_port="/dev/ttyUSB2")
        self.assertEqual(usb["FARSIGHT_CAMERA"], "/dev/video0")
        self.assertIn("/dev/video0", launch.device_paths(usb))
        self.assertIn("/dev/ttyUSB2", launch.build_args(usb))

    def test_phone_profile_accepts_actual_rgb_camera_packets(self):
        import cv2
        import json
        import struct
        import time
        import numpy as np
        from fastapi.testclient import TestClient
        from jetson.mapping.server import create_app
        env = render(camera_source="phone", serial_port="")
        app = create_app(camera_device=env["FARSIGHT_CAMERA"] or None, detector="none")
        _, encoded = cv2.imencode(".jpg", np.zeros((12, 16, 3), np.uint8))
        jpeg = encoded.tobytes()
        header = json.dumps(dict(version=1, type="rgb_frame", session_id="phone-profile",
                                 frame_id=0, t_capture=1, jpeg_len=len(jpeg))).encode()
        with TestClient(app) as client, client.websocket_connect("/phone/rgb") as ws:
            ws.send_json(dict(version=1, type="hello", session_id="phone-profile", map_epoch=1,
                              device="profile test", supports_scene_depth=False))
            ws.send_bytes(struct.pack("<I", len(header)) + header + jpeg)
            deadline = time.monotonic() + 2
            while True:
                health = client.get("/health").json()
                if health["camera"]:
                    break
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            self.assertEqual(health["camera_source"], "PHONE RGB")
            self.assertFalse(health["rover"])

    def test_quoted_repo_src_env_and_model_paths_remain_single_arguments(self):
        repo, src, envfile = "/home/jet/project space", "/home/jet/src space", "/etc/farsight config.env"
        selection = envgen.model_selection(repo, exists=lambda value: True)
        for unit in ("farsight.service", "farsight-llm.service", "farsight-whisper.service"):
            text = envgen.render_unit(EXAMPLE.with_name(unit).read_text(), repo, src, "jet", selection, envfile)
            exec_line = next(line for line in text.splitlines() if line.startswith("ExecStart="))
            expected = f"{repo}/jetson/.venv/bin/python" if unit == "farsight.service" else f"{src}/{'llama.cpp' if 'llm' in unit else 'whisper.cpp'}/build/bin/{'llama-server' if 'llm' in unit else 'whisper-server'}"
            self.assertEqual(shlex.split(exec_line.partition("=")[2])[0], expected)
            file_line = next(line for line in text.splitlines() if line.startswith("EnvironmentFile="))
            self.assertEqual(file_line.partition("=")[2], envfile)
            working = next(line for line in text.splitlines() if line.startswith("WorkingDirectory="))
            self.assertEqual(working.partition("=")[2], repo)
            # Boot must not wait on network-online.target (networkd-wait-online held it for 2 min).
            self.assertFalse([line for line in text.splitlines()
                              if line.startswith(("After=", "Wants=", "Requires=")) and "network-online" in line])
            defaults = {}
            for line in text.splitlines():
                if line.startswith("Environment="):
                    assignment = shlex.split(line.partition("=")[2])[0]
                    name, _, value = assignment.partition("=")
                    defaults[name] = value
            argv = shlex.split(exec_line.partition("=")[2])
            argv = [defaults.get(item[2:-1], item) if item.startswith("${") and item.endswith("}") else item for item in argv]
            if unit == "farsight-llm.service":
                self.assertEqual(argv[argv.index("--image-max-tokens") + 1], "256")
                self.assertEqual(argv[argv.index("--cache-ram") + 1], "0")  # the 8 GiB default exhausts the board
                self.assertIn("MemoryMax=4608M", text)
                self.assertEqual(argv[argv.index("-m") + 1], selection["FARSIGHT_LLM_MODEL"])
                self.assertEqual(argv[argv.index("--mmproj") + 1], selection["FARSIGHT_LLM_MMPROJ"])
            elif unit == "farsight-whisper.service":
                self.assertEqual(argv[argv.index("-m") + 1], selection["FARSIGHT_WHISPER_SERVER_MODEL"])
            self.assertNotIn("@REPO@", text)
        rendered = envgen.render(EXAMPLE.read_text(), repo, exists=lambda value: True, camera_source="phone", serial_port="")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.env"
            path.write_text(rendered)
            actual = launch.read_env_file(path)
        self.assertEqual(actual["FARSIGHT_LLM_MODEL"], selection["FARSIGHT_LLM_MODEL"])

    def test_nav_unit_waits_for_a_live_pose_before_launching_nav2(self):
        # Nav2's lifecycle bringup aborts without base_link TF for ~60 s; a boot before the phone connects
        # would strand it until a manual restart.
        text = EXAMPLE.with_name("farsight-nav.service").read_text()
        pre = next(line for line in text.splitlines() if line.startswith("ExecStartPre="))
        argv = shlex.split(pre.partition("=")[2])
        self.assertEqual(argv[:2], ["/bin/bash", "-c"])
        self.assertIn("/nav/status", argv[2])
        self.assertIn("pose_source.:.arkit", argv[2])
        subprocess.run(["bash", "-n", "-c", argv[2]], check=True)
        self.assertIn("TimeoutStartSec=infinity", text.splitlines())

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
