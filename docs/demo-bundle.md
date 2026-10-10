# Local USB/offline demo bundle

`tools/package_demo.py` creates one tar containing a clean committed source
snapshot, a verified Quest build, and only the model families you explicitly
select. It never installs dependencies, deploys to a machine, starts services,
uses SSH, signs an app, or flashes hardware.

Run the small fixture tests first:

```sh
python -m unittest tools.tests.test_package_demo -v
```

Before creating a real bundle, finish and commit the source changes. Dirty or
untracked source is refused. Existing ignored model/cache folders are not swept
into the archive. Use a stable checkout while another teammate measures latency.

For the prepared phone/pose/assistant path:

```sh
python -m tools.package_demo create \
  --output /tmp/farsight-demo.tar --build \
  --model-family pose \
  --model-family vision-smol \
  --model-family whisper-tiny \
  --model-family piper-low
```

Default compression is **none**, avoiding CPU work recompressing quantized
weights. `--compression gzip` is available when wanted, with an appropriate
`.tar.gz` filename. The tool prints the finished archive SHA-256, size, source
commit, verification result, and honest capability flags. Models can make the
archive large; hashing/copying/verification are streamed in bounded chunks.

`--build` runs the known existing `tsc --noEmit && vite build` npm script in
offline mode, without npm install and without pre/post script hooks. Node/npm
and the existing local node_modules must already be installed. It writes the
ignored `quest/dist` assets and a build stamp, then checks the source is still
clean. Local/private Quest `.env*` inputs and `VITE_*` environment inputs are
refused because they are not part of the committed build snapshot.

Without `--build`, `quest/dist/.farsight-build.json` must already match the
committed Quest source fingerprint and the complete asset hash inventory. A
missing stamp, source change, missing/extra asset, or edited asset fails rather
than packaging an uncertain build. Backend-only commits can reuse a matching
Quest build because its fingerprint follows committed Quest inputs.

## Explicit artifact selection

Repeat `--model-family` as needed:

| Family | Required artifacts under `models/` |
| --- | --- |
| `detection` | `yolo11n.onnx`, labels JSON, export manifest |
| `pose` | `yolo11n-pose.onnx`, labels JSON, export manifest |
| `vision-smol` | SmolVLM-500M Q8 model and its matching named Q8 projector |
| `vision-qwen` | Qwen2.5-VL-3B Q4 model and its matching named Q8 projector |
| `whisper-tiny`, `whisper-base`, `whisper-medium` | Corresponding `ggml-*.en.bin` |
| `piper-low` | Root-level lessac-low ONNX voice and its `.onnx.json` |
| `piper-medium` | `piper/` lessac-medium voice and its `.onnx.json` |
| `piper-spanish` | `piper/` es_MX-claude-high voice and its `.onnx.json` |
| `face` | `version-RFB-320.onnx` |
| `depth` | Metric indoor small Depth Anything ONNX |

No model family is selected automatically. A requested missing/empty file or
partial named vision pair fails before an archive is published. Detection/pose
weights must match their local prepared export manifest's filename, SHA-256,
and byte count. The family mapping cannot combine a Smol model with a Qwen
projector. No TensorRT `.engine` or `.plan` is packaged; build those on the target
Jetson as described in `docs/inference-setup.md`.

The inventory preserves supplied export source/license metadata. Other prepared
weights have computed SHA-256 and recognized source links in the bundle
inventory, with license/trusted-export verification explicitly unknown rather
than invented. Matching names and hashes establish package contents; they do
not prove model quality, compatibility, download authenticity, or licensing.
Use the provenance notes in `models/README.md` for the individual weights.

## Verify after transfer

Using this trusted source checkout's stdlib-only helper, on the sender and then
the receiver:

```sh
python -m tools.package_demo verify /tmp/farsight-demo.tar
# On the receiver, use the actual mounted USB path:
python -m tools.package_demo verify /media/USB/farsight-demo.tar
```

The verifier streams archive members without extracting them. It rejects
missing/truncated, duplicate, extra, mismatched, symlink/nonregular, absolute,
and traversal paths, plus incomplete selected model families and disallowed
binary/engine or capability claims. It also checks the complete tar layout so
truncated padding is not mistaken for an intact generated archive. Copy the
published archive SHA-256 alongside the handoff and compare it independently;
the JSON inventory is an integrity record, not a cryptographic signature.

The finished archive has a `farsight/` root. `farsight/bundle-manifest.json` is
first and records every payload path, kind, byte count, and SHA-256. Source
comes from Git blobs at the recorded commit, not a working-directory sweep.
Secret/config/key files, Git metadata, virtual environments, node_modules,
generated caches, native executable headers, and portable-engine assumptions
are excluded or refused. Excluded committed paths are recorded in the inventory.

Creation uses a temporary file in the output directory, verifies it, confirms
the source commit stayed stable, and only then atomically publishes the final
filename. A failure cleans up the temporary archive. Existing output is refused
unless `--force` was explicitly supplied; even then, an interrupted/missing
artifact failure preserves the previous output. No source file can be overwritten
as a bundle output.

## What the bundle does not provide

The capability flags explicitly say Python/npm dependencies, native Jetson
binaries, TensorRT engines, iOS signing, flashed firmware, offline dependency
installation, and hardware qualification are **not** provided. The Uno sketch
and native iPhone project are source artifacts when present in the commit; that
does not mean they are built, installed, signed, or flashed.

Preinstall dependencies and build target-native executables separately. Mac
executables cannot be used on the Jetson. Verify the transferred package, then
follow the documented core-first phone/camera and motor bench handoff. Copying
a tar is not proof that phone routing, Quest rendering, video age, cellular
capacity, motor stopping, power, or thermals work on the physical rig.
