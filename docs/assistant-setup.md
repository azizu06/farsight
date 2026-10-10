# Local voice and vision assistant setup

The default assistant is an accurately labelled summary of detections, camera
freshness, and range. Optional local models add vision questions, speech
recognition, and speech synthesis. Answers never execute motors. Optional proposals require explicit confirmation
and fresh held operator commands.

Default stack (Jetson Orin Nano 8 GB, shared with pose TensorRT, H.264 and the server):

| Artifact | Purpose |
|---|---|
| `Qwen3VL-2B-Instruct-Q4_K_M.gguf` (1.11 GB) | Vision language model, official `Qwen/Qwen3-VL-2B-Instruct-GGUF` |
| `mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf` (445 MB) | Its matching image projector (model + projector 1.55 GB) |
| `ggml-base.en.bin` (148 MB) | Local English Whisper transcription (`ggml-tiny.en.bin` is the fallback) |
| `en_US-lessac-low.onnx` and `.onnx.json` (or `piper/en_US-lessac-medium.onnx`) | Local Piper voice |

Fetch the vision pair with `python3 -m tools.fetch_models --groups vlm-qwen3`
(SHA-256 checked). Exact files, hashes and URLs are in [`models/README.md`](../models/README.md).
`SmolVLM-500M-Instruct-Q8_0.gguf` + `mmproj-SmolVLM-500M-Instruct-Q8_0.gguf` and
`Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf` + `mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf` remain
supported pairs.

### Model choice (measured on the Mac, not the Jetson)

Same `SceneAssistant.ask()` code path the server's `/ask` uses, same camera JPEGs and
detector facts for every model: two questions ("What do you see?", "Is anyone hurt or in
danger?") on five scenes (two street/people photos, a rescue-rubble photo, a living room,
and the simulated replay frame), cold run plus a warm second pass. `llama-server` build 11146
(Homebrew llama.cpp 0.5.0, commit `7fe450e19`), Apple M5 Pro 48 GB, Metal, `-c 2048 -ngl 99
--parallel 1`. Peak RSS is from `/usr/bin/time -l` over the whole server run. Metal
unified memory and mmap make RSS a loose proxy for Jetson CUDA memory, so Jetson numbers
must be measured on the Jetson.

| Model + projector | Files | `--image-max-tokens` | Cold ask | Warm median / max | Peak RSS |
|---|---|---|---|---|---|
| Qwen3-VL-2B Q4_K_M + Q8_0 mmproj | 1.55 GB | 256 (shipped) | 747 ms | **548 / 977 ms** | **2.7 GB** |
| Qwen3-VL-2B Q4_K_M + Q8_0 mmproj | 1.55 GB | model default | 1508 ms | 1169 / 1664 ms | 3.8 GB |
| Qwen2.5-VL-3B Q4_K_M + Q8_0 mmproj | 2.77 GB | 256 | 1084 ms | 537 / 832 ms | 3.0 GB |
| Qwen2.5-VL-3B Q4_K_M + Q8_0 mmproj | 2.77 GB | model default | 2103 ms | 1573 / 2001 ms | 3.9 GB |
| SmolVLM-500M Q8_0 + Q8_0 mmproj | 0.55 GB | model default | 436 ms | 231 / 305 ms | 0.97 GB |

Answer quality, judged by reading the 10 warm answers per model:

- **SmolVLM-500M** returned the same caption for both questions on every image. It never
  answers "is anyone hurt", so it is a speed fallback only.
- **Qwen2.5-VL-3B** answers tersely and correctly, but the hurt-or-danger question always
  became a flat "no evidence of anyone being hurt", with no scene detail.
- **Qwen3-VL-2B** gives the richest scene description (rescue workers, crane, rug and
  sofa, orange and teal blocks), uses the supplied detector facts and range, and says
  when nobody is visible. It is wordier ("Based on the image...") and, with the default
  image-token budget, one danger answer on the two-person photo rambled; the two-sentence
  cap in `ask_intent.guard_answer` bounds that. At 256 image tokens, answers were as good
  and the latency and memory halved, so the unit passes `--image-max-tokens 256`
  (llama.cpp warns that Qwen-VL grounding tasks want 1024; the assistant only describes
  and answers questions).
- Five scenes and ten prompts are a small judged sample, not a benchmark. Hurt-person
  recognition from a low rover camera is not shown by any of these runs.

Verdict: Qwen3-VL-2B is not worse than the 3B model for this use, its files are 1.2 GB
smaller, and it is the default when its pair is present. Its Mac peak RSS (2.7 GB)
includes mapped weights and Metal buffers, so it does not by itself show the Jetson
process stays near 2 GB; measure that on the rig. Keep SmolVLM as the speed fallback;
Qwen2.5-VL-3B only if the Jetson has memory headroom and its terse answers are preferred.

### Jetson memory profiles (measured on the Orin Nano 8 GB)

Measured 2026-10-10 with CUDA (`sm_87`) builds of llama.cpp and whisper.cpp, `-c 2048 -ngl 99
--parallel 1 --image-max-tokens 256`, `farsight.service` running (about 0.9 GB RSS), a saved
camera frame (the phone was unplugged, so not the live camera), `max_tokens` 80. TTFT is time
to the first streamed token; RSS is the whole process (Jetson memory is unified, so it includes
the GPU buffers). Free RAM is `free -m` "available" after dropping caches.

| Vision model (Q4_K_M + Q8 projector) | RSS | TTFT | Full answer | Verdict |
|---|---|---|---|---|
| Qwen3-VL-2B (default) | 2.77 GB | 0.24 to 0.78 s | 1.3 to 3.1 s | runs with the desktop on or off |
| Qwen2.5-VL-3B | 3.75 GB | 0.5 to 1.2 s | 1.2 to 2.7 s (terse) | loads only with the desktop off, alone |
| Qwen3-VL-4B | 4.29 GB | 0.4 to 1.5 s | 2.6 to 6.3 s | loads only with the desktop off, alone; misses the 4 s target |

| Whisper | RSS after use | Short clip (warm / cold) |
|---|---|---|
| `ggml-base.en.bin` (default) | 0.6 to 0.8 GB | 0.24 s / 0.76 s |
| `ggml-small.en.bin` | 1.0 to 1.2 GB | 0.42 s / 0.52 s |

End to end through `SceneAssistant.ask()` (Qwen3-VL-2B + base.en + Piper lessac-low, desktop on):
STT 0.22 to 0.35 s, vision 1.3 to 2.0 s, Piper 0.3 to 1.1 s, total 2.0 to 3.4 s warm.

Memory math (7.49 GB total):

- Desktop off, `farsight.service` and ROS tooling idle, no assistant: 4.9 GB available.
- Plus the default 2B + base.en pair: 2.8 GB available. Nav2 (about 1 GB) and nvblox (about
  1 GB) fit with roughly 0.8 GB spare.
- Same with the desktop on: 0.7 to 1.6 GB available, so Nav2 and nvblox do not fit. Run the rover headless.
- `headless` profile (2B + `small.en`): 0.4 GB less than the default, so about 0.4 GB spare
  after Nav2 and nvblox. Better transcription, still 0.42 s.
- 3B and 4B are not a profile: they fit only alone. 4B plus `small.en` plus `farsight.service`
  (about 7 GB) pushed the Jetson into swap-less thrash until it stopped answering ssh and
  needed a power cycle. Do not run them next to Whisper, the server, or ROS.

`bash jetson/assistant_profile.sh default|headless|status` switches the Whisper model with a
systemd drop-in and restarts only `farsight-whisper`/`farsight-llm`. `headless` refuses to
run while the desktop is up. Turn the desktop off with `sudo systemctl isolate multi-user.target`
(this leaves the USB `192.168.55.1` bridge without a DHCP lease for the Mac; give the Mac
interface a manual `192.168.55.100/24`).

Both servers must be CUDA builds. An existing `llama-server`/`whisper-server` without
`build/bin/libggml-cuda.so*` runs on the CPU (about 13 prompt tokens/s for the VLM) and
`setup.sh`/`install_service.sh` now say so; rebuild with `-DGGML_CUDA=ON
-DCMAKE_CUDA_ARCHITECTURES=87` and `-j2` while ROS or the server is running.

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

The installer prefers the first **complete** vision pair in this order:
Qwen3-VL-2B Q4_K_M + Q8 projector, then SmolVLM-500M Q8 + Q8 projector, then
Qwen2.5-VL-3B Q4_K_M + Q8 projector. Whisper order is `ggml-base.en.bin`, then
`ggml-tiny.en.bin`, then medium. Voices: the root-level lessac-low voice, then the
existing `models/piper/en_US-lessac-medium.onnx` layout. It never pairs a model with
another model's projector: a model file without its own projector is skipped, and an
explicit mixed pair is reported as not ready. It only enables a
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

Run the local model server on the Jetson (the installed unit uses port 8092):

```sh
llama-server -m models/Qwen3VL-2B-Instruct-Q4_K_M.gguf \
  --mmproj models/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf \
  --alias local --host 127.0.0.1 --port 8082 -c 2048 -ngl 99 --parallel 1 \
  --image-max-tokens 256
```

For SmolVLM or Qwen2.5-VL substitute that model's own file pair from the table above.

The [llama.cpp multimodal guide](https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md)
describes model/projector loading. Add these options to the normal rover server
command, using the actual executable paths on the Jetson:

```sh
  --assistant-url http://127.0.0.1:8082 \
  --whisper-bin /path/to/whisper-cli --whisper-model models/ggml-base.en.bin \
  --piper-bin /path/to/piper --piper-model models/en_US-lessac-low.onnx \
  --tts-dir /tmp/farsight-tts
```

These are additional flags, not a standalone shell command. Install
`piper-tts` in the chosen environment when using its Python executable. FFmpeg
converts microphone WebM/WAV to 16 kHz mono WAV; transcription is local only: the
whisper-server at `--whisper-url` (`FARSIGHT_WHISPER_URL`, the installed default) or, without it, whisper-cli. No browser cloud speech-recognition API is used.

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

## Exact Jetson steps (Orin Nano 8 GB, JetPack with CUDA, sm_87)

`bash jetson/setup.sh --with-vlm` does the builds and fetches the Qwen3 pair (the builds
are skipped with `--skip-builds`); the manual form, on the Jetson only:

```sh
# 1. llama.cpp and whisper.cpp built natively with CUDA for Orin (compute capability 8.7)
cd ~/src
git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF
cmake --build build --config Release -j2 --target llama-server    # 20-40 min; -j2 avoids swapping on 8 GB
cd ~/src && git clone https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j2 --target whisper-server whisper-cli

# 2. Models (from the Mac; the files are not in git)
tools/push_models.sh --fetch --groups piper,face,whisper,vlm-qwen3 USER@JETSON
#   needs on the Jetson, in ~/farsight/models/:
#   Qwen3VL-2B-Instruct-Q4_K_M.gguf, mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf, ggml-base.en.bin,
#   and a Piper voice (en_US-lessac-low.onnx or piper/en_US-lessac-medium.onnx)

# 3. Units and env (writes the four lines below into /etc/farsight.env)
bash jetson/install_service.sh
```

The env lines the installer writes (edit them to switch models; re-run the installer so the
unit defaults follow):

```sh
FARSIGHT_LLM_MODEL=/home/USER/farsight/models/Qwen3VL-2B-Instruct-Q4_K_M.gguf
FARSIGHT_LLM_MMPROJ=/home/USER/farsight/models/mmproj-Qwen3VL-2B-Instruct-Q8_0.gguf
FARSIGHT_WHISPER_SERVER_MODEL=/home/USER/farsight/models/ggml-base.en.bin
FARSIGHT_ASSISTANT_URL=http://127.0.0.1:8092
FARSIGHT_WHISPER_URL=http://127.0.0.1:8091
```

Not measured yet on the Jetson: resident memory of the llama-server process with the pose
engine and H.264 running, ask latency, and pose fps under assistant load. Check
`tegrastats` and `python -m tools.measure ask` during the rig rehearsal; if memory is tight,
fall back to SmolVLM by setting both `FARSIGHT_LLM_MODEL` and `FARSIGHT_LLM_MMPROJ` to the
Smol pair.

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
