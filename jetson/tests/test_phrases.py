import asyncio
from pathlib import Path
import tempfile
import unittest
import wave

from fastapi.testclient import TestClient

from jetson.mapping.server import create_app
from jetson.phrases import PHRASES, PhraseSpeaker, SpeechError


def fake_synthesize(calls):
    def synthesize(model, text, path):
        calls.append((model, text))
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 160)
    return synthesize


class FakePlayer:
    def __init__(self):
        self.played = []

    async def __call__(self, args):
        self.played.append(args)


class SpeakerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.en, self.es = root / "en.onnx", root / "es.onnx"
        self.en.write_bytes(b"voice")
        self.es.write_bytes(b"voice")
        self.calls, self.player = [], FakePlayer()

    def speaker(self, speaker_cmd="aplay -q", es=True):
        return PhraseSpeaker(Path(self.directory.name) / "tts", voices=dict(en=self.en, es=self.es if es else None),
                             speaker_cmd=speaker_cmd, synthesize=fake_synthesize(self.calls), play=self.player)

    def test_phrase_set_has_english_and_spanish(self):
        self.assertIn("help_coming", PHRASES)
        for phrase in PHRASES.values():
            self.assertTrue(phrase["en"] and phrase["es"])
        self.assertEqual(PHRASES["help_coming"]["es"], "La ayuda viene en camino, no se mueva.")

    def test_speaker_present_plays_on_rover_and_returns_url(self):
        result = asyncio.run(self.speaker().speak("help_coming", "es"))
        self.assertTrue(result["played"])
        self.assertEqual(result["text"], PHRASES["help_coming"]["es"])
        self.assertEqual(result["audio_url"], "/tts/phrase-help_coming-es.wav")
        self.assertEqual(self.calls, [(str(self.es.resolve()), PHRASES["help_coming"]["es"])])
        self.assertEqual(self.player.played[0][:2], ["aplay", "-q"])
        self.assertTrue(self.player.played[0][2].endswith("phrase-help_coming-es.wav"))

    def test_rendered_phrase_is_cached(self):
        speaker = self.speaker()
        asyncio.run(speaker.speak("help_coming", "en"))
        asyncio.run(speaker.speak("help_coming", "en"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.player.played), 2)

    def test_speaker_absent_returns_url_for_headset(self):
        speaker = self.speaker(speaker_cmd="none")
        result = asyncio.run(speaker.speak("help_coming", "en"))
        self.assertFalse(result["played"])
        self.assertEqual(result["audio_url"], "/tts/phrase-help_coming-en.wav")
        self.assertEqual(self.player.played, [])
        self.assertFalse(speaker.status()["speaker"])

    def test_unknown_phrase_and_language_are_rejected(self):
        with self.assertRaises(SpeechError) as unknown:
            asyncio.run(self.speaker().speak("open_the_door", "en"))
        self.assertEqual(unknown.exception.status_code, 404)
        with self.assertRaises(SpeechError) as language:
            asyncio.run(self.speaker().speak("help_coming", "fr"))
        self.assertEqual(language.exception.status_code, 400)

    def test_missing_spanish_voice_is_a_clear_error(self):
        speaker = self.speaker(es=False)
        with self.assertRaises(SpeechError) as missing:
            asyncio.run(speaker.speak("help_coming", "es"))
        self.assertEqual(missing.exception.status_code, 503)
        self.assertIn("Spanish", str(missing.exception))
        self.assertIn("FARSIGHT_PIPER_MODEL_ES", str(missing.exception))
        self.assertEqual(speaker.status()["voices"], ["en"])

    def test_player_failure_falls_back_to_headset(self):
        async def broken(args):
            raise FileNotFoundError(args[0])
        speaker = self.speaker()
        speaker.play = broken
        result = asyncio.run(speaker.speak("help_coming", "en"))
        self.assertFalse(result["played"])
        self.assertIn("speaker_warning", result)

    def test_auto_detects_speaker_with_probe(self):
        speaker = PhraseSpeaker(self.directory.name, voices={}, speaker_cmd="auto",
                                probe=lambda: ["aplay", "-q"])
        self.assertEqual(speaker.status()["speaker"], True)
        self.assertEqual(speaker.status()["speaker_cmd"], "aplay -q")
        silent = PhraseSpeaker(self.directory.name, voices={}, speaker_cmd="auto", probe=lambda: None)
        self.assertFalse(silent.status()["speaker"])


class SpeakEndpointTests(unittest.TestCase):
    def test_speak_routes_health_and_tts_file(self):
        with tempfile.TemporaryDirectory() as directory:
            voice = Path(directory) / "en.onnx"
            voice.write_bytes(b"voice")
            app = create_app(detector="none", piper_model=str(voice), tts_dir=str(Path(directory) / "tts"),
                             speaker_cmd="none")
            calls = []
            app.state.phrases.synthesize = fake_synthesize(calls)
            with TestClient(app) as client:
                listing = client.get("/speak").json()
                self.assertIn("help_coming", [item["id"] for item in listing["phrases"]])
                self.assertFalse(listing["speaker"])
                result = client.post("/speak", json=dict(phrase="help_coming", lang="en"))
                self.assertEqual(result.status_code, 200, result.text)
                self.assertFalse(result.json()["played"])
                audio = client.get(result.json()["audio_url"])
                self.assertEqual(audio.headers["content-type"], "audio/wav")
                self.assertEqual(client.post("/speak", json=dict(phrase="nope", lang="en")).status_code, 404)
                missing = client.post("/speak", json=dict(phrase="help_coming", lang="es"))
                self.assertEqual(missing.status_code, 503)
                self.assertIn("Spanish", missing.json()["detail"])
                self.assertEqual(client.post("/speak", json=dict(phrase="help_coming")).status_code, 422)
                health = client.get("/health").json()
                self.assertEqual(health["speech"]["speaker"], False)
                self.assertEqual(health["speech"]["voices"], ["en"])
                self.assertIn("triage", health)

    def test_triage_endpoint_returns_template_without_model(self):
        import cv2
        import numpy as np
        import time
        from jetson.camera import CameraFrame
        app = create_app(detector="none")
        with TestClient(app) as client:
            self.assertEqual(client.post("/mission/triage/PHONE%20RGB:7").status_code, 404)
            _, jpeg = cv2.imencode(".jpg", np.zeros((8, 8, 3), np.uint8))
            app.state.mission.detect(dict(boxes=[dict(id=7, cls="person", conf=.6, x=0, y=0, w=.5, h=.5, down=True)]),
                                     CameraFrame(jpeg.tobytes(), time.monotonic(), 1, "PHONE RGB"))
            response = client.post("/mission/triage/PHONE%20RGB:7")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["model"], "template")
            report = client.get("/mission/report").json()
            self.assertIn("down", report["triage"]["PHONE RGB:7"]["assessment"].lower())


if __name__ == "__main__":
    unittest.main()
