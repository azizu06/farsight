"""Build/verify an explicit offline-transfer bundle; never install or deploy.

Create: python -m tools.package_demo create --output /tmp/farsight-demo.tar --build
Verify: python -m tools.package_demo verify /tmp/farsight-demo.tar
"""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "farsight"
MANIFEST = f"{PREFIX}/bundle-manifest.json"
BUILD_STAMP = ".farsight-build.json"
BUILD_SCRIPT = "tsc --noEmit && vite build"
CHUNK = 1024 * 1024
MAX_FILE = 8 * 1024**3
MAX_TOTAL = 32 * 1024**3
MAX_METADATA = 8 * 1024**2
MAX_FILES = 20000
DISABLED_CAPABILITIES = ("python_dependencies", "node_dependencies", "native_jetson_binaries", "tensorrt_engines",
                         "ios_signing", "firmware_flashed", "hardware_qualified", "offline_dependency_installation_ready")

MODEL_FAMILIES = {
    "detection": dict(files=("yolo11n.onnx", "yolo11n.labels.json", "yolo11n.manifest.json"),
                      export_manifest="yolo11n.manifest.json", source="https://github.com/ultralytics/assets"),
    "pose": dict(files=("yolo11n-pose.onnx", "yolo11n-pose.labels.json", "yolo11n-pose.manifest.json"),
                 export_manifest="yolo11n-pose.manifest.json", source="https://github.com/ultralytics/assets"),
    "vision-smol": dict(files=("SmolVLM-500M-Instruct-Q8_0.gguf", "mmproj-SmolVLM-500M-Instruct-Q8_0.gguf"),
                        source="https://huggingface.co/ggml-org/SmolVLM-500M-Instruct-GGUF", paired=True),
    "vision-qwen": dict(files=("Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf", "mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf"),
                        source="https://huggingface.co/ggml-org/Qwen2.5-VL-3B-Instruct-GGUF", paired=True),
    "whisper-tiny": dict(files=("ggml-tiny.en.bin",), source="https://huggingface.co/ggerganov/whisper.cpp"),
    "whisper-base": dict(files=("ggml-base.en.bin",), source="https://huggingface.co/ggerganov/whisper.cpp"),
    "whisper-medium": dict(files=("ggml-medium.en.bin",), source="https://huggingface.co/ggerganov/whisper.cpp"),
    "piper-low": dict(files=("en_US-lessac-low.onnx", "en_US-lessac-low.onnx.json"),
                      source="https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/lessac/low"),
    "piper-medium": dict(files=("piper/en_US-lessac-medium.onnx", "piper/en_US-lessac-medium.onnx.json"),
                         source="https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/lessac/medium"),
    "piper-spanish": dict(files=("piper/es_MX-claude-high.onnx", "piper/es_MX-claude-high.onnx.json"),
                          source="https://huggingface.co/rhasspy/piper-voices/tree/main/es/es_MX/claude/high"),
    "face": dict(files=("version-RFB-320.onnx",), source="https://huggingface.co/onnxmodelzoo/version-RFB-320"),
    "depth": dict(files=("depth_anything_v2_metric_indoor_small_308x406.onnx",),
                  source="https://huggingface.co/depth-anything/Depth-Anything-V2-Metric-Indoor-Small-hf"),
}


class BundleError(RuntimeError):
    pass


def safe_name(name):
    if (not isinstance(name, str) or not name or len(name) > 4096 or name.startswith("/")
            or "\\" in name or re.match(r"^[A-Za-z]:", name)
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or any(part in ("", ".", "..") for part in name.split("/"))):
        raise BundleError(f"Unsafe archive/source path: {name!r}")
    return name


def excluded_path(name):
    path = PurePosixPath(safe_name(name))
    if any(part in (".git", ".venv", "venv", "node_modules", "__pycache__", ".ssh", ".aws", "xcuserdata") for part in path.parts):
        return "development cache, repository metadata, or private configuration"
    if path.parts[0] == "models" and path.name != "README.md":
        return "model artifacts must be explicitly selected"
    if name.startswith(("rust/target/", "ios/build/", "ios/DerivedData/")) or path.name == ".DS_Store":
        return "generated build cache"
    if name.startswith("quest/dist/"):
        return "Quest assets are taken from the verified build separately"
    if (path.name.startswith(".env") and not path.name.endswith(".example")
            or path.name in ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "credentials.json", "service-account.json", "service_account.json")
            or path.name.endswith(".env")):
        return "private environment/credential file"
    if path.suffix.lower() in (".pem", ".key", ".p12", ".pfx", ".p8", ".engine", ".plan", ".pyc", ".pyo",
                                ".dylib", ".so", ".dll", ".exe", ".ipa", ".xcarchive"):
        return "secret, native binary, generated engine, or cache"
    return None


def native_binary(prefix):
    return prefix[:4] in (b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf",
                          b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",
                          b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca") or prefix[:2] == b"MZ"


def git(repo, *args):
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError(f"Cannot read committed Git snapshot: {type(exc).__name__}") from exc


def clean_snapshot(repo, ignored=()):
    ignored = set(ignored)
    dirty = []
    for item in git(repo, "status", "--porcelain=v1", "--untracked-files=all", "-z").split(b"\0"):
        if item:
            name = item[3:].decode("utf-8", errors="strict")
            if name not in ignored:
                dirty.append(name)
    if dirty:
        raise BundleError("Repository has dirty/untracked source; commit or remove it before packaging: " + ", ".join(dirty[:8]))
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise BundleError("A committed snapshot is required")
    return commit


@dataclass
class Payload:
    path: str
    size: int
    sha256: str
    kind: str
    mode: int = 0o644
    blob: str | None = None
    local: Path | None = None

    def record(self):
        return dict(path=self.path, size=self.size, sha256=self.sha256, kind=self.kind)


class BlobReader:
    """Read immutable committed blobs from one Git process, bounded per read."""
    def __init__(self, repo):
        self.process = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    @contextmanager
    def open(self, blob, size):
        self.process.stdin.write((blob + "\n").encode())
        self.process.stdin.flush()
        header = self.process.stdout.readline(200).split()
        if len(header) != 3 or header[1] != b"blob" or int(header[2]) != size:
            raise BundleError("Committed blob shape/size changed unexpectedly")
        remaining = size
        class Reader:
            def read(inner, count=CHUNK):
                nonlocal remaining
                count = min(CHUNK, remaining, count if count >= 0 else CHUNK)
                data = self.process.stdout.read(count) if count else b""
                remaining -= len(data)
                if count and not data:
                    raise BundleError("Committed Git blob was truncated")
                return data
        try:
            yield Reader()
        finally:
            while remaining:
                chunk = self.process.stdout.read(min(CHUNK, remaining))
                if not chunk:
                    raise BundleError("Committed Git blob was truncated")
                remaining -= len(chunk)
            if self.process.stdout.read(1) != b"\n":
                raise BundleError("Invalid committed Git blob terminator")

    def close(self):
        self.process.stdin.close()
        self.process.stdout.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def digest_reader(reader, limit=MAX_FILE):
    digest, size, prefix = hashlib.sha256(), 0, b""
    while chunk := reader.read(CHUNK):
        if not prefix:
            prefix = chunk[:4]
        digest.update(chunk)
        size += len(chunk)
        if size > limit:
            raise BundleError("Artifact exceeds its bounded size limit")
    return digest.hexdigest(), size, prefix


def committed_payloads(repo, commit):
    payloads, excluded = [], []
    blobs = BlobReader(repo)
    try:
        for item in git(repo, "ls-tree", "-r", "-l", "-z", commit).split(b"\0"):
            if not item:
                continue
            metadata, raw_name = item.split(b"\t", 1)
            mode, kind, blob, size = metadata.split()
            name = safe_name(raw_name.decode("utf-8"))
            if mode == b"120000" or kind != b"blob":
                raise BundleError(f"Committed symlinks/submodules are not supported: {name}")
            reason = excluded_path(name)
            if reason:
                excluded.append(dict(path=name, reason=reason))
                continue
            count = int(size)
            if not 0 <= count <= MAX_FILE:
                raise BundleError(f"Committed file is excessive: {name}")
            with blobs.open(blob.decode(), count) as source:
                digest, actual, prefix = digest_reader(source)
            if native_binary(prefix):
                excluded.append(dict(path=name, reason="native executable excluded from source bundle"))
                continue
            payloads.append(Payload(f"{PREFIX}/{name}", actual, digest, "committed_source",
                                    0o755 if mode == b"100755" else 0o644, blob=blob.decode()))
    finally:
        blobs.close()
    return payloads, excluded


@contextmanager
def regular_file(path, root):
    path, root = Path(path), Path(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise BundleError("Artifact lies outside its declared folder") from exc
    current = root
    if root.is_symlink():
        raise BundleError(f"Symlink artifact folder rejected: {root}")
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise BundleError(f"Symlink artifact rejected: {current}")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise BundleError(f"Required artifact is missing/unreadable: {path}") from exc
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 <= info.st_size <= MAX_FILE:
            raise BundleError(f"Required artifact is not a bounded regular file: {path}")
        yield source


def local_payload(path, folder, archive_path, kind):
    safe_name(archive_path)
    with regular_file(path, folder) as source:
        digest, size, prefix = digest_reader(source)
    if native_binary(prefix):
        raise BundleError(f"Native executable is not an allowed artifact: {path}")
    if kind == "model" and not size:
        raise BundleError(f"Requested model artifact is empty: {path}")
    return Payload(archive_path, size, digest, kind, local=path)


def quest_fingerprint(payloads):
    fields = [(item.path.removeprefix(f"{PREFIX}/"), item.sha256, item.size)
              for item in payloads if item.path.startswith(f"{PREFIX}/quest/")]
    if not any(name == "quest/package.json" for name, _, _ in fields):
        raise BundleError("Committed Quest package.json is missing")
    return hashlib.sha256(json.dumps(sorted(fields), separators=(",", ":")).encode()).hexdigest()


def quest_assets(repo):
    folder = repo / "quest/dist"
    if not folder.is_dir() or folder.is_symlink():
        raise BundleError("Built quest/dist is absent or a symlink; use --build after installing dependencies")
    assets = []
    for base, directories, files in os.walk(folder, followlinks=False):
        for name in directories:
            if (Path(base) / name).is_symlink():
                raise BundleError("Quest build contains a symlink directory")
        for name in files:
            if name == BUILD_STAMP:
                continue
            path = Path(base) / name
            relative = path.relative_to(folder).as_posix()
            if excluded_path(relative):
                raise BundleError(f"Disallowed file in Quest build: {relative}")
            assets.append(local_payload(path, folder, f"{PREFIX}/quest/dist/{relative}", "quest_asset"))
    if not any(item.path == f"{PREFIX}/quest/dist/index.html" for item in assets):
        raise BundleError("Quest build index.html is missing")
    return sorted(assets, key=lambda item: item.path)


def write_build_stamp(repo, payloads):
    fingerprint = quest_fingerprint(payloads)
    package = json.loads((repo / "quest/package.json").read_text())
    if package.get("scripts", {}).get("build") != BUILD_SCRIPT:
        raise BundleError("Unknown Quest build script; only the current tsc --noEmit && vite build is supported")
    if any(path.is_file() and not path.name.endswith(".example") for path in (repo / "quest").glob(".env*")):
        raise BundleError("Ignored/private Quest .env build inputs are not part of the committed snapshot")
    if any(key.startswith("VITE_") for key in os.environ):
        raise BundleError("VITE_* environment build inputs are not part of the committed snapshot")
    try:
        subprocess.run(["npm", "--offline", "--ignore-scripts", "--prefix", str(repo / "quest"), "run", "build"],
                       cwd=repo, check=True, timeout=180, env={**os.environ, "npm_config_offline": "true"})
    except (OSError, subprocess.SubprocessError) as exc:
        raise BundleError("Offline Quest build failed; install dependencies separately before packaging") from exc
    assets = quest_assets(repo)
    stamp = dict(schema=1, source_fingerprint=fingerprint, command=BUILD_SCRIPT,
                 assets=[item.record() for item in assets])
    target = repo / "quest/dist" / BUILD_STAMP
    with tempfile.NamedTemporaryFile("w", dir=target.parent, prefix=".farsight-build-", delete=False) as output:
        temporary = Path(output.name)
        json.dump(stamp, output, sort_keys=True)
    os.replace(temporary, target)


def verified_quest_assets(repo, payloads, build=False):
    if build:
        write_build_stamp(repo, payloads)
    target = repo / "quest/dist" / BUILD_STAMP
    with regular_file(target, repo / "quest/dist") as source:
        data = source.read(MAX_METADATA + 1)
    if len(data) > MAX_METADATA:
        raise BundleError("Quest build stamp is excessive")
    try:
        stamp = json.loads(data)
    except ValueError as exc:
        raise BundleError("Quest build stamp is invalid; use --build") from exc
    assets = quest_assets(repo)
    if (stamp.get("schema") != 1 or stamp.get("source_fingerprint") != quest_fingerprint(payloads)
            or stamp.get("command") != BUILD_SCRIPT or stamp.get("assets") != [item.record() for item in assets]):
        raise BundleError("Quest source/build mismatch or changed assets; use --build on the clean committed source")
    return assets, dict(source_fingerprint=stamp["source_fingerprint"], command=stamp["command"], assets=len(assets))


def selected_models(repo, families):
    folder, payloads, provenance = repo / "models", [], []
    for family in sorted(set(families)):
        if family not in MODEL_FAMILIES:
            raise BundleError(f"Unknown requested model family: {family}")
        definition = MODEL_FAMILIES[family]
        selected = [local_payload(folder / name, folder, f"{PREFIX}/models/{name}", "model") for name in definition["files"]]
        entry = dict(family=family, source=definition["source"], files=[item.record() for item in selected],
                     matching_named_vision_pair=bool(definition.get("paired")),
                     trusted_export_manifest_verified=False, license=None)
        if definition.get("export_manifest"):
            path = folder / definition["export_manifest"]
            with regular_file(path, folder) as source:
                data = source.read(MAX_METADATA + 1)
            if len(data) > MAX_METADATA:
                raise BundleError("Export manifest is excessive")
            try:
                metadata = json.loads(data)
            except ValueError as exc:
                raise BundleError(f"Invalid prepared export manifest: {family}") from exc
            model = selected[0]
            if (not isinstance(metadata, dict) or metadata.get("model") != definition["files"][0]
                    or type(metadata.get("bytes")) is not int or metadata.get("bytes") != model.size
                    or metadata.get("sha256") != model.sha256):
                raise BundleError(f"Prepared export manifest mismatch: {family}")
            entry.update(trusted_export_manifest_verified=True, license=metadata.get("license"),
                         export_source=metadata.get("source"))
        provenance.append(entry)
        payloads.extend(selected)
    return payloads, provenance


class HashingReader:
    def __init__(self, source):
        self.source, self.digest, self.size = source, hashlib.sha256(), 0
    def read(self, count=CHUNK):
        chunk = self.source.read(min(CHUNK, count if count >= 0 else CHUNK))
        self.digest.update(chunk)
        self.size += len(chunk)
        return chunk


def add_payload(archive, item, source):
    info = tarfile.TarInfo(item.path)
    info.size, info.mode, info.mtime = item.size, item.mode, 0
    reader = HashingReader(source)
    archive.addfile(info, reader)
    if reader.size != item.size or reader.digest.hexdigest() != item.sha256:
        raise BundleError(f"Payload changed during packaging: {item.path}")


def expected_tar_size(inventory_size, files):
    total = 0
    for name, size in [(MANIFEST, inventory_size)] + [(item["path"], item["size"]) for item in files]:
        info = tarfile.TarInfo(name)
        info.size, info.mtime = size, 0
        total += len(info.tobuf(format=tarfile.PAX_FORMAT)) + ((size + 511) // 512) * 512
    return ((total + tarfile.RECORDSIZE - 1) // tarfile.RECORDSIZE) * tarfile.RECORDSIZE


class CountingReader:
    def __init__(self, source):
        self.source, self.count = source, 0
    def read(self, size=-1):
        data = self.source.read(size)
        self.count += len(data)
        return data


@contextmanager
def archive_input(path):
    with Path(path).open("rb") as source:
        magic = source.read(2)
        source.seek(0)
        if magic == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=source, mode="rb") as compressed:
                yield compressed
        else:
            yield source


def verify_bundle(path):
    try:
        with archive_input(path) as input_stream:
            stream = CountingReader(input_stream)
            with tarfile.open(fileobj=stream, mode="r|", ignore_zeros=True) as archive:
                return _verify_archive(archive, stream)
    except BundleError:
        raise
    except (OSError, EOFError, ValueError, KeyError, TypeError, tarfile.TarError) as exc:
        raise BundleError(f"Unreadable, truncated, or invalid bundle: {type(exc).__name__}") from exc


def _verify_archive(archive, stream):
    first = archive.next()
    if first is None or first.name != MANIFEST or not first.isfile() or not 0 < first.size <= MAX_METADATA:
        raise BundleError("Bundle inventory must be the first bounded regular file")
    manifest = json.loads(archive.extractfile(first).read(MAX_METADATA + 1))
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), list) or len(manifest["files"]) > MAX_FILES:
        raise BundleError("Unsupported or excessive bundle inventory")
    caps = manifest.get("capabilities", {})
    if any(caps.get(name) is not False for name in DISABLED_CAPABILITIES):
        raise BundleError("Bundle capability claims must not imply dependencies, binaries, signing, or hardware qualification")
    if (caps.get("committed_source") is not True or caps.get("quest_build_verified") is not True
            or not isinstance(manifest.get("source_commit"), str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", manifest["source_commit"])):
        raise BundleError("Committed source/build provenance is missing")
    families = caps.get("selected_model_families")
    if not isinstance(families, list) or any(not isinstance(name, str) or name not in MODEL_FAMILIES for name in families) or len(families) != len(set(families)):
        raise BundleError("Invalid selected model families")
    expected = {}
    total = 0
    for item in manifest["files"]:
        name = safe_name(item["path"])
        if not name.startswith(PREFIX + "/") or name == MANIFEST or name in expected:
            raise BundleError("Duplicate/outside/reserved bundle inventory path")
        if type(item["size"]) is not int or not 0 <= item["size"] <= MAX_FILE or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise BundleError("Invalid payload size or SHA-256")
        relative = name.removeprefix(PREFIX + "/")
        if item.get("kind") not in ("committed_source", "quest_asset", "model"):
            raise BundleError("Unknown inventory payload kind")
        if item["kind"] == "model":
            allowed = {"models/" + filename for family in caps.get("selected_model_families", [])
                       if family in MODEL_FAMILIES for filename in MODEL_FAMILIES[family]["files"]}
            if relative not in allowed:
                raise BundleError("Model artifact is not in an explicitly selected recognized family")
        elif item["kind"] == "quest_asset":
            if not relative.startswith("quest/dist/") or excluded_path(relative.removeprefix("quest/dist/")):
                raise BundleError("Disallowed Quest asset inventory path")
        elif excluded_path(relative):
            raise BundleError("Disallowed source inventory path")
        total += item["size"]
        if total > MAX_TOTAL:
            raise BundleError("Bundle payload budget exceeded")
        expected[name] = item
    required_models = {f"{PREFIX}/models/{name}" for family in families for name in MODEL_FAMILIES[family]["files"]}
    actual_models = {name for name, item in expected.items() if item["kind"] == "model"}
    if actual_models != required_models:
        raise BundleError("Requested model family is incomplete or has unexpected artifacts")
    seen = set()
    while (member := archive.next()) is not None:
        name = safe_name(member.name)
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or name not in expected or name in seen:
            raise BundleError(f"Extra, duplicate, or nonregular bundle member: {name}")
        item = expected[name]
        if member.size != item["size"]:
            raise BundleError(f"Bundle member size mismatch: {name}")
        digest, size, prefix = digest_reader(archive.extractfile(member))
        if size != item["size"] or digest != item["sha256"]:
            raise BundleError(f"Bundle member SHA-256 mismatch: {name}")
        if native_binary(prefix) or name.endswith((".engine", ".plan")):
            raise BundleError("Disallowed native binary/engine in bundle")
        seen.add(name)
    if seen != set(expected):
        raise BundleError("Bundle members are missing: " + ", ".join(sorted(set(expected) - seen)[:8]))
    if stream.count != expected_tar_size(first.size, manifest["files"]):
        raise BundleError("Bundle tar stream is truncated or contains extra trailing data")
    return dict(verified=True, source_commit=manifest.get("source_commit"), files=len(expected),
            payload_bytes=total, capabilities=manifest.get("capabilities"), models=manifest.get("models"))

def create_bundle(repo, output, families=(), build=False, force=False, compression="none"):
    repo, output = Path(repo).resolve(), Path(output).absolute()
    if output.is_symlink() or output.exists() and not force:
        raise BundleError("Output already exists/is a symlink; use another filename or explicit --force")
    if not output.parent.is_dir():
        raise BundleError("Output parent directory must already exist")
    if compression not in ("none", "gzip"):
        raise BundleError("Compression must be none or gzip")
    ignored = []
    try:
        relative_output = output.relative_to(repo).as_posix()
        if relative_output.encode() in git(repo, "ls-files", "-z").split(b"\0"):
            raise BundleError("Bundle output must not overwrite a committed file")
        ignored.append(relative_output)
    except ValueError:
        pass
    commit = clean_snapshot(repo, ignored)
    sources, excluded = committed_payloads(repo, commit)
    assets, build_info = verified_quest_assets(repo, sources, build=build)
    models, provenance = selected_models(repo, families)
    if clean_snapshot(repo, ignored) != commit:
        raise BundleError("Source commit changed during preparation")
    payloads = sorted(sources + assets + models, key=lambda item: item.path)
    if len(payloads) > MAX_FILES or len({item.path for item in payloads}) != len(payloads) or any(item.path == MANIFEST for item in payloads):
        raise BundleError("Excessive, duplicate, or reserved package filenames")
    total = sum(item.size for item in payloads)
    if total > MAX_TOTAL:
        raise BundleError("Bundle exceeds the 32 GiB payload budget")
    manifest = dict(schema=1, source_commit=commit, created_at_utc=datetime.now(timezone.utc).isoformat(),
                    build=build_info, compression=compression, files=[item.record() for item in payloads], models=provenance,
                    excluded_committed_files=excluded, capabilities=dict(committed_source=True,
                        quest_build_verified=True, selected_model_families=sorted(set(families)),
                        python_dependencies=False, node_dependencies=False, native_jetson_binaries=False,
                        tensorrt_engines=False, ios_signing=False, firmware_flashed=False, hardware_qualified=False,
                        offline_dependency_installation_ready=False))
    metadata = json.dumps(manifest, indent=2, sort_keys=True).encode()
    if len(metadata) > MAX_METADATA:
        raise BundleError("Bundle inventory is excessive")
    temporary = None
    blobs = BlobReader(repo)
    try:
        with tempfile.NamedTemporaryFile("wb", dir=output.parent, prefix=".farsight-bundle-", suffix=".tmp", delete=False) as target:
            temporary = Path(target.name)
            with tarfile.open(fileobj=target, mode="w|gz" if compression == "gzip" else "w|", format=tarfile.PAX_FORMAT) as archive:
                inventory = tarfile.TarInfo(MANIFEST)
                inventory.size, inventory.mode, inventory.mtime = len(metadata), 0o644, 0
                archive.addfile(inventory, io.BytesIO(metadata))
                for item in payloads:
                    if item.blob:
                        with blobs.open(item.blob, item.size) as source:
                            add_payload(archive, item, source)
                    else:
                        folder = repo / ("models" if item.kind == "model" else "quest/dist")
                        with regular_file(item.local, folder) as source:
                            add_payload(archive, item, source)
            target.flush()
            os.fsync(target.fileno())
        verification = verify_bundle(temporary)
        extra_ignored = list(ignored)
        try:
            extra_ignored.append(temporary.relative_to(repo).as_posix())
        except ValueError:
            pass
        if clean_snapshot(repo, extra_ignored) != commit:
            raise BundleError("Source changed while bundling; retry from a stable clean commit")
        with temporary.open("rb") as source:
            digest, size, _ = digest_reader(source, limit=MAX_TOTAL + MAX_METADATA * 2)
        os.replace(temporary, output)
        temporary = None
        return dict(path=str(output), archive_sha256=digest, archive_bytes=size, **verification)
    finally:
        blobs.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="mode", required=True)
    create = commands.add_parser("create")
    create.add_argument("--repo", type=Path, default=ROOT)
    create.add_argument("--output", required=True, type=Path)
    create.add_argument("--model-family", action="append", choices=sorted(MODEL_FAMILIES), default=[])
    create.add_argument("--build", action="store_true", help="Run the known existing npm build offline; never install packages")
    create.add_argument("--force", action="store_true", help="Atomically replace an existing output only after full verification")
    create.add_argument("--compression", choices=("none", "gzip"), default="none", help="Default is uncompressed tar to avoid recompressing quantized weights")
    verify = commands.add_parser("verify")
    verify.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    try:
        report = (create_bundle(args.repo, args.output, args.model_family, args.build, args.force, args.compression)
                  if args.mode == "create" else verify_bundle(args.archive))
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except BundleError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
