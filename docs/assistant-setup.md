# Local voice and vision assistant setup

The default assistant is an accurately labelled summary of detections, camera
freshness, and range. Optional local models add vision questions, speech
recognition, and speech synthesis. Answers never execute motors. Optional proposals require explicit confirmation
and fresh held operator commands.

Prepared on the development Mac before the hardware test:

| Artifact | Purpose |
|---|---|
| `SmolVLM-500M-Instruct-Q8_0.gguf` | Small local vision language model |
| `mmproj-SmolVLM-500M-Instruct-Q8_0.gguf` | Matching image projector |
| `ggml-tiny.en.bin` | Local English Whisper transcription |
| `en_US-lessac-low.onnx` and `.onnx.json` | Local Piper voice |

Models are in the ignored `models/` directory; copying the source repository
alone does not copy them. Use `tools/push_models.sh USER@JETSON` or a USB drive.
The [model and projector](https://huggingface.co/ggml-org/SmolVLM-500M-Instruct-GGUF/tree/main),
[Whisper models](https://huggingface.co/ggerganov/whisper.cpp/tree/main), and
[Piper voice](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/lessac/low)
must be downloaded before an offline startup.

Build or install llama.cpp and whisper.cpp for the actual Jetson; Mac binaries
cannot run on Jetson Linux. Use the optional assistant/model build phase of
`jetson/setup.sh` after the core rover path works. The installer runs native
builds as `farsight-llm.service` (on port 8092) and `farsight-whisper.service`
(on port 8091), wiring
`FARSIGHT_ASSISTANT_URL`/`FARSIGHT_WHISPER_URL` in `/etc/farsight.env`; the manual
commands below are for rehearsal on other machines. Latency commands: `python -m tools.measure ask`
([jetson-bringup.md](jetson-bringup.md) step 11). Avoid rebuilding or reflashing a
working JetPack installation just to enable this optional feature.

The installer now prefers the actual prepared artifacts: a **complete**
SmolVLM-500M/Q8 pair, tiny English Whisper, and the root-level lessac-low voice.
If absent, it supports the complete Qwen2.5-VL-3B Q4/model + Q8/projector pair,
base or medium Whisper, and the existing `models/piper/en_US-lessac-medium.onnx`
layout. It never pairs the Smol model with the Qwen projector. It only enables a
model unit when the selected files and target-native executable exist; path
checks are not evidence of successful Jetson inference.

For the initial phone-camera service pass with physical motors disabled:

```sh
bash jetson/install_service.sh --camera-source phone --serial-port ''
```

`phone` blanks `FARSIGHT_CAMERA`, so the launcher omits `--camera` and does not
wait for `/dev/video0`; `/phone/rgb` can then own the camera. Default `usb`
behavior is preserved. Existing `/etc/farsight.env` is kept: if an **explicitly
requested** camera or serial selection differs, the installer refuses to modify
or start services until you edit the config or repeat the same profile with
`--reset-env`, which backs up the old file before rendering that profile.
Leaving `--camera-source` unspecified does not reject an existing phone config
just because the automatic default is USB. After
lifted-wheel firmware and motor tests, select the **identified** real serial
port explicitly and use `--reset-env` if changing an existing config.

Model overrides are `--llm-model PATH`, `--llm-projector PATH`,
`--whisper-model PATH`, and `--piper-model PATH`. Corresponding config keys are
`FARSIGHT_LLM_MODEL`, `FARSIGHT_LLM_MMPROJ`, `FARSIGHT_WHISPER_SERVER_MODEL` (or
the existing `FARSIGHT_WHISPER_MODEL`), and `FARSIGHT_PIPER_MODEL`. Nonempty
explicit paths override automatic choices; an incomplete/mixed known vision
pair is reported and is never silently replaced. A recognized model with no
explicit projector searches only for its matching projector in the same folder.
For renamed/custom artifacts, provide both paths and validate their compatibility
on the Jetson. Re-run the installer after changing unit model selection, since
the selected paths become quoted unit defaults. Spaces in repo, build, config,
and model paths remain separate arguments; no env file is sourced as shell code.

Run the local model server on the Jetson:

```sh
llama-server -m models/SmolVLM-500M-Instruct-Q8_0.gguf \
  --mmproj models/mmproj-SmolVLM-500M-Instruct-Q8_0.gguf \
  --alias local --host 127.0.0.1 --port 8082 -c 2048 -ngl 99 --parallel 1
```

The [llama.cpp multimodal guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)
describes model/projector loading. Add these options to the normal rover server
command, using the actual executable paths on the Jetson:

```sh
  --assistant-url http://127.0.0.1:8082 \
  --whisper-bin /path/to/whisper-cli --whisper-model models/ggml-tiny.en.bin \
  --piper-bin /path/to/piper --piper-model models/en_US-lessac-low.onnx \
  --tts-dir /tmp/farsight-tts
```

These are additional flags, not a standalone shell command. Install
`piper-tts` in the chosen environment when using its Python executable. FFmpeg
converts microphone WebM/WAV to 16 kHz mono WAV; only local whisper-cli performs
transcription. No browser cloud speech-recognition API is used.

In Quest settings, type a question or explicitly select Ask by voice. Recording
stops after ten seconds or when cancelled/dismissed. Camera controls remain
stopped while settings are open. A reply can be played from the rover's WAV;
local browser speech is an explicit fallback when available. In VR, after
**Enable in-VR voice** in settings, holding the left trigger records the same
`/ask` upload; see [Quest controls](../quest/README.md#controls).

Software validation on the Mac used the actual downloaded SmolVLM and Piper to
answer a bundled test photo and produce speech in approximately 5.4 seconds.
Actual Piper speech saying “What do you see?” was transcribed by the compiled
whisper-cli and tiny English model as that same question. These results prove
the local model path on the Mac, not Jetson throughput or Quest microphone access.

VLM requests have a twelve-second deadline. Audio uploads are capped at 8 MiB
and conversion at twenty seconds. Whisper and Piper have bounded subprocess
timeouts; cancellation kills and reaps children. Speech retains thirty replies.
One question runs at a time, so requests cannot create a model backlog.

At the hardware rehearsal, test model startup, a real image question, Quest
microphone permission, transcription, playback, CPU/RAM/temperature, and control
lease timing with the assistant active. Leave optional models off if they cause
video stalls. A model outage visibly falls back to the observation summary;
missing Whisper returns 503 for audio while typed questions still work.

## SBAR triage and survivor phrases

The same `--assistant-url` model writes an SBAR note (Situation, Background,
Assessment, Recommendation) for each new person track, from a crop of its
snapshot plus confidence, `down` flag, range and time. A template note is stored
first, so the mission report always has one; model calls run on one background
thread at least `--triage-interval` seconds apart. Mac Metal, Qwen2.5-VL-3B Q4:
median ~1.0 s per note. The Quest mission report shows the note under each track.

Survivor phrases (`POST /speak`, see `docs/interfaces.md`) need `--piper-model`
(English) and `--piper-model-es` (Spanish, `es_MX-claude-high` from setup.sh).
Plug a USB speaker into the Jetson; with `--speaker-cmd auto` the server finds it
through `aplay -l`. Without one, the headset plays the phrase. Note: the
piper-tts macOS wheel cannot find its espeak-ng data when the venv path is very
long (~170+ characters); use a short venv path.
