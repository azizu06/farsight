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
cannot run on Jetson Linux. The existing `jetson/setup.sh` includes source builds
but may take substantial time and installs system packages. Inspect that script
before choosing it during the hardware test. Avoid rebuilding or reflashing a
working JetPack installation just to enable this optional feature.

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
local browser speech is an explicit fallback when available.

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
