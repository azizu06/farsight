"""Preset survivor phrases in English and Spanish, spoken on the rover with Piper.

Phrases are a fixed set (never free text), rendered once per language into the TTS
directory and played through the Jetson's default ALSA/Pulse output when a speaker
is present. Without a speaker the caller gets the WAV URL so the headset can play it.
"""

import asyncio
from contextlib import suppress
from pathlib import Path
import shlex
import shutil
import subprocess
import time
import wave

PHRASES = {
    "help_coming": dict(en="Help is coming, stay still.", es="La ayuda viene en camino, no se mueva."),
    "found_you": dict(en="We found you. A rescuer is on the way.", es="Lo encontramos. Un rescatista va en camino."),
    "can_you_hear": dict(en="Can you hear me? Raise your hand if you can.",
                         es="¿Me escucha? Levante la mano si puede."),
    "call_out": dict(en="If you can, call out so we can find you.", es="Si puede, grite para que podamos encontrarlo."),
    "stay_calm": dict(en="Stay calm. You are not alone.", es="Mantenga la calma. No está solo."),
}
LANGUAGES = dict(en=("English", "FARSIGHT_PIPER_MODEL"), es=("Spanish", "FARSIGHT_PIPER_MODEL_ES"))


class SpeechError(Exception):
    def __init__(self, message, status_code=503):
        super().__init__(message)
        self.status_code = status_code


def probe_speaker():
    """Default output command when the Jetson has a playback device, else None."""
    if shutil.which("aplay"):
        try:
            cards = subprocess.run(["aplay", "-l"], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        return ["aplay", "-q"] if cards.returncode == 0 and "card" in cards.stdout else None
    if shutil.which("paplay") and shutil.which("pactl"):
        try:
            sinks = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        return ["paplay"] if sinks.returncode == 0 and sinks.stdout.strip() else None
    return None


class PhraseSpeaker:
    PROBE_TTL = 30
    SPEECH_TIMEOUT = 20

    def __init__(self, output_dir, voices, piper_bin=None, speaker_cmd="auto", synthesize=None,
                 play=None, probe=probe_speaker):
        self.output_dir = Path(output_dir).resolve() if output_dir else None
        self.voices = {lang: Path(path).expanduser() for lang, path in (voices or {}).items() if path}
        self.piper_bin = piper_bin
        self.speaker_cmd = (speaker_cmd or "auto").strip()
        self.synthesize = synthesize or self._synthesize
        self.play = play or self._play
        self.probe = probe
        self._probed, self._probed_at = None, None
        self._loaded = {}
        self._player = None
        self._lock = asyncio.Lock()

    def output(self):
        if self.speaker_cmd.lower() in ("none", "off", ""):
            return None
        if self.speaker_cmd.lower() != "auto":
            return shlex.split(self.speaker_cmd)
        if self._probed_at is None or time.monotonic() - self._probed_at > self.PROBE_TTL:
            self._probed, self._probed_at = self.probe(), time.monotonic()
        return self._probed

    def voice(self, lang):
        path = self.voices.get(lang)
        return str(path.resolve()) if path and path.is_file() and path.stat().st_size > 0 else None

    def status(self):
        output = self.output()
        return dict(speaker=bool(output), speaker_cmd=" ".join(output) if output else None,
                    voices=[lang for lang in LANGUAGES if self.voice(lang)])

    def phrases(self):
        return [dict(id=key, **text) for key, text in PHRASES.items()]

    async def speak(self, phrase, lang):
        if phrase not in PHRASES:
            raise SpeechError(f"Unknown phrase {str(phrase)[:40]!r}", 404)
        if lang not in LANGUAGES:
            raise SpeechError("Language must be 'en' or 'es'", 400)
        name, variable = LANGUAGES[lang]
        model = self.voice(lang)
        if not model:
            raise SpeechError(f"No {name} Piper voice installed; set {variable} to a downloaded .onnx voice "
                              "(jetson/setup.sh fetches them)")
        if self.output_dir is None:
            raise SpeechError("Phrase speech requires a TTS output directory")
        text = PHRASES[phrase][lang]
        filename = f"phrase-{phrase}-{lang}.wav"
        path = self.output_dir / filename
        async with self._lock:
            if not self._valid(path):
                await self._render(model, text, path)
        result = dict(phrase=phrase, lang=lang, text=text, audio_url=f"/tts/{filename}", played=False)
        output = self.output()
        if output:
            try:
                await self.play([*output, str(path)])
                result["played"] = True
            except (OSError, asyncio.TimeoutError) as exc:
                result["speaker_warning"] = f"Rover speaker failed ({type(exc).__name__}); play audio_url on the headset"
        return result

    @staticmethod
    def _valid(path):
        try:
            with wave.open(str(path), "rb") as recording:
                return recording.getnframes() > 0
        except (OSError, EOFError, wave.Error):
            return False

    async def _render(self, model, text, path):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        try:
            if self.piper_bin and self.synthesize == self._synthesize:
                process = await asyncio.create_subprocess_exec(
                    self.piper_bin, "--model", model, "--output_file", str(temporary),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                try:
                    await asyncio.wait_for(process.communicate((text + "\n").encode("utf-8")), self.SPEECH_TIMEOUT)
                finally:
                    if process.returncode is None:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
            else:
                await asyncio.wait_for(asyncio.to_thread(self.synthesize, model, text, temporary), self.SPEECH_TIMEOUT)
            if not self._valid(temporary):
                raise SpeechError("Piper did not produce a phrase WAV")
            temporary.replace(path)
        except (OSError, asyncio.TimeoutError, wave.Error) as exc:
            raise SpeechError(f"Piper phrase rendering failed ({type(exc).__name__})") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _synthesize(self, model, text, path):
        if model not in self._loaded:
            try:
                from piper import PiperVoice
            except ImportError as exc:
                raise OSError("Phrase speech requires pip install piper-tts or --piper-bin") from exc
            self._loaded[model] = PiperVoice.load(model)
        with wave.open(str(path), "wb") as output:
            self._loaded[model].synthesize_wav(text, output)

    async def _play(self, args):
        # A new phrase interrupts the previous one; playback runs on without blocking the request.
        await self.close()
        self._player = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                            stdout=asyncio.subprocess.DEVNULL,
                                                            stderr=asyncio.subprocess.DEVNULL)

    async def close(self):
        player, self._player = self._player, None
        if player and player.returncode is None:
            with suppress(ProcessLookupError):
                player.kill()
            await player.wait()
