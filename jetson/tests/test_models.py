"""GET /health models: on-rover model names, Hugging Face repo ids, loaded state and last latency."""
from fastapi.testclient import TestClient

import jetson.models
from jetson.mapping.server import create_app
from jetson.models import identify, inventory, unit_model


def test_catalog_names_hub_repos_and_marks_yolo_not_hugging_face():
    assert identify("/m/Qwen3VL-2B-Instruct-Q4_K_M.gguf") == ("Qwen3-VL-2B", "huggingface", "Qwen/Qwen3-VL-2B-Instruct-GGUF")
    assert identify("/m/ggml-base.en.bin") == ("Whisper base.en", "huggingface", "ggerganov/whisper.cpp")
    assert identify("/m/piper/es_MX-claude-high.onnx") == ("Piper es_MX-claude-high", "huggingface", "rhasspy/piper-voices")
    name, source, repo = identify("/m/yolo11n-pose.engine")
    assert (name, source) == ("YOLO11n-pose", "ultralytics") and "not Hugging Face" in repo
    assert identify("/m/custom.onnx") == ("custom.onnx", "local", None)
    assert identify(None) is None


def test_unit_model_reads_the_installed_service_environment(tmp_path):
    (tmp_path / "farsight-llm.service").write_text(
        '[Service]\nEnvironment="FARSIGHT_SELECTED_LLM_MODEL=/home/u/models/Qwen3VL-2B-Instruct-Q4_K_M.gguf"\n')
    assert unit_model("farsight-llm.service", "FARSIGHT_SELECTED_LLM_MODEL", tmp_path) == "/home/u/models/Qwen3VL-2B-Instruct-Q4_K_M.gguf"
    assert unit_model("farsight-whisper.service", "FARSIGHT_SELECTED_WHISPER_MODEL", tmp_path) is None


def test_inventory_skips_unconfigured_models():
    rows = inventory(detector=("/m/yolo11n-pose.engine", True, 21.5), voices=[("tts", None, False, None)])
    assert rows == [dict(role="detector", name="YOLO11n-pose", source="ultralytics", repo=rows[0]["repo"],
                         file="yolo11n-pose.engine", loaded=True, last_ms=21.5)]


def test_health_lists_service_models_with_the_latest_ask_timings(tmp_path, monkeypatch):
    (tmp_path / "farsight-llm.service").write_text('Environment="FARSIGHT_SELECTED_LLM_MODEL=/m/Qwen3VL-2B-Instruct-Q4_K_M.gguf"\n')
    (tmp_path / "farsight-whisper.service").write_text('Environment="FARSIGHT_SELECTED_WHISPER_MODEL=/m/ggml-base.en.bin"\n')
    monkeypatch.setattr(jetson.models, "UNIT_DIR", tmp_path)
    app = create_app(demo=True, simulate_rover=True, detector="hog",
                     assistant_url="http://127.0.0.1:8092", whisper_url="http://127.0.0.1:8091")
    with TestClient(app) as client:
        app.state.assistant.last_timings.update(stt=304, vlm=912)
        models = {row["role"]: row for row in client.get("/health").json()["models"]}
    assert models["vision"] == dict(role="vision", name="Qwen3-VL-2B", source="huggingface",
                                    repo="Qwen/Qwen3-VL-2B-Instruct-GGUF", file="Qwen3VL-2B-Instruct-Q4_K_M.gguf",
                                    loaded=True, last_ms=912)
    assert (models["stt"]["name"], models["stt"]["repo"], models["stt"]["last_ms"]) == ("Whisper base.en", "ggerganov/whisper.cpp", 304)
    assert "detector" not in models  # bundled HOG has no model file
