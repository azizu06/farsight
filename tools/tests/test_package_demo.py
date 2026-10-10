"""Small local fixture bundles only; no real model compression or deployment."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools import package_demo as bundle


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="farsight-bundle-tests-")
        self.root = Path(self.work.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.run_git("init", "-q")
        files = {
            ".gitignore": "quest/dist/\nmodels/*\n!models/README.md\nnode_modules/\n.venv/\n",
            "README.md": "Farsight fixture source\n",
            "quest/package.json": json.dumps({"scripts": {"build": bundle.BUILD_SCRIPT}}),
            "quest/package-lock.json": "{}",
            "quest/src/main.js": "console.log('committed');\n",
            "models/README.md": "Model provenance lives separately\n",
            "rover/farsight_uno.ino": "// Firmware source only; not flashed\n",
        }
        for name, text in files.items():
            self.write(name, text.encode())
        self.commit()
        self.write("quest/dist/index.html", b"<script src='assets/view.js'></script>")
        self.write("quest/dist/assets/view.js", b"console.log('built');")
        self.stamp()
        self.output = self.root / "demo.tar"

    def tearDown(self):
        self.work.cleanup()

    def run_git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "core.hooksPath=/dev/null", *args],
                              check=True, capture_output=True).stdout

    def commit(self):
        self.run_git("add", "-A")
        self.run_git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture")

    def write(self, name, data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def stamp(self):
        commit = self.run_git("rev-parse", "HEAD").decode().strip()
        sources, _ = bundle.committed_payloads(self.repo, commit)
        assets = bundle.quest_assets(self.repo)
        data = dict(schema=1, source_fingerprint=bundle.quest_fingerprint(sources), command=bundle.BUILD_SCRIPT,
                    assets=[item.record() for item in assets])
        self.write("quest/dist/" + bundle.BUILD_STAMP, json.dumps(data).encode())

    def models(self, families):
        for family in families:
            definition = bundle.MODEL_FAMILIES[family]
            for name in definition["files"]:
                self.write("models/" + name, b"fixture-model " + name.encode())
            if definition.get("export_manifest"):
                first = definition["files"][0]
                payload = (self.repo / "models" / first).read_bytes()
                metadata = dict(model=first, sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload),
                                task=family, source="local fixture", license="fixture only")
                self.write("models/" + definition["export_manifest"], json.dumps(metadata).encode())

    def read_archive(self, path=None):
        with tarfile.open(path or self.output) as archive:
            return [(member.name, archive.extractfile(member).read()) for member in archive.getmembers()]

    def mutated_archive(self, mutate):
        entries = self.read_archive()
        entries = mutate(entries)
        path = self.root / "mutated.tar"
        with tarfile.open(path, "w", format=tarfile.PAX_FORMAT) as archive:
            for name, data in entries:
                item = tarfile.TarInfo(name)
                item.size, item.mtime = len(data), 0
                archive.addfile(item, io.BytesIO(data))
        return path

    def test_plain_and_gzip_round_trip_have_honest_capabilities_and_selected_models(self):
        families = ["pose", "vision-smol", "whisper-tiny", "piper-low"]
        self.models(families)
        for compression in ("none", "gzip"):
            with self.subTest(compression=compression):
                output = self.root / ("demo.tar" if compression == "none" else "demo.tar.gz")
                report = bundle.create_bundle(self.repo, output, families=families, compression=compression)
                self.assertTrue(report["verified"])
                self.assertEqual(bundle.verify_bundle(output)["files"], report["files"])
                self.assertEqual(report["archive_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
                for key in bundle.DISABLED_CAPABILITIES:
                    self.assertFalse(report["capabilities"][key], key)
                self.assertEqual(report["capabilities"]["selected_model_families"], sorted(families))
                provenance = {entry["family"]: entry for entry in report["models"]}
                self.assertTrue(provenance["pose"]["trusted_export_manifest_verified"])
                self.assertTrue(provenance["vision-smol"]["matching_named_vision_pair"])
                self.assertFalse(provenance["vision-smol"]["trusted_export_manifest_verified"])
                self.assertIsNone(provenance["vision-smol"]["license"])

    def test_only_committed_source_and_safe_generated_assets_are_bundled(self):
        self.write("private.env", b"never bundle this secret")
        self.write(".env.production", b"never bundle private build variables")
        self.write("tls.key", b"private key")
        self.write("mac-helper", b"\xcf\xfa\xed\xfe" + b"native executable")
        self.write("old.engine", b"not portable")
        self.commit()
        self.stamp()
        bundle.create_bundle(self.repo, self.output)
        entries = dict(self.read_archive())
        for name in ("private.env", ".env.production", "tls.key", "mac-helper", "old.engine"):
            self.assertNotIn(f"farsight/{name}", entries)
        self.assertIn("farsight/rover/farsight_uno.ino", entries)
        self.assertNotIn("farsight/.git/config", entries)
        manifest = json.loads(entries[bundle.MANIFEST])
        self.assertEqual(len(manifest["excluded_committed_files"]), 5)

    def test_dirty_and_untracked_source_and_stale_build_fail_before_output(self):
        self.write("quest/src/main.js", b"changed source")
        with self.assertRaisesRegex(bundle.BundleError, "dirty"):
            bundle.create_bundle(self.repo, self.output)
        self.commit()
        with self.assertRaisesRegex(bundle.BundleError, "source/build mismatch"):
            bundle.create_bundle(self.repo, self.output)
        self.stamp()
        self.write("untracked.py", b"ignored source must not sneak into bundle")
        with self.assertRaisesRegex(bundle.BundleError, "untracked"):
            bundle.create_bundle(self.repo, self.output)
        self.assertFalse(self.output.exists())

    def test_changed_extra_or_missing_quest_asset_is_not_silently_bundled(self):
        self.write("quest/dist/assets/view.js", b"tampered build")
        with self.assertRaisesRegex(bundle.BundleError, "changed assets"):
            bundle.create_bundle(self.repo, self.output)
        self.stamp()
        self.write("quest/dist/extra.js", b"unverified")
        with self.assertRaisesRegex(bundle.BundleError, "changed assets"):
            bundle.create_bundle(self.repo, self.output)
        (self.repo / "quest/dist/extra.js").unlink()
        (self.repo / "quest/dist/index.html").unlink()
        with self.assertRaisesRegex(bundle.BundleError, "index.html"):
            bundle.create_bundle(self.repo, self.output)

    def test_missing_requested_pair_and_manifest_mismatch_preserve_existing_output(self):
        self.models(["vision-smol"])
        (self.repo / "models" / bundle.MODEL_FAMILIES["vision-smol"]["files"][1]).unlink()
        self.output.write_bytes(b"old published package")
        with self.assertRaisesRegex(bundle.BundleError, "missing"):
            bundle.create_bundle(self.repo, self.output, families=["vision-smol"], force=True)
        self.assertEqual(self.output.read_bytes(), b"old published package")
        self.models(["pose"])
        self.write("models/yolo11n-pose.onnx", b"modified weights")
        with self.assertRaisesRegex(bundle.BundleError, "manifest mismatch"):
            bundle.create_bundle(self.repo, self.output, families=["pose"], force=True)
        self.assertEqual(self.output.read_bytes(), b"old published package")
        self.assertEqual(list(self.root.glob(".farsight-bundle-*")), [])

    def test_interrupted_copy_never_publishes_or_leaves_partial_archive(self):
        self.output.write_bytes(b"old published package")
        with patch("tools.package_demo.add_payload", side_effect=RuntimeError("interrupted")), self.assertRaises(RuntimeError):
            bundle.create_bundle(self.repo, self.output, force=True)
        self.assertEqual(self.output.read_bytes(), b"old published package")
        self.assertEqual(list(self.root.glob(".farsight-bundle-*")), [])

    def test_verifier_rejects_missing_extra_changed_duplicate_and_truncated_members(self):
        bundle.create_bundle(self.repo, self.output)
        for modify in (lambda values: values[:-1], lambda values: values + [("farsight/extra.txt", b"extra")],
                       lambda values: values[:-1] + [(values[-1][0], b"changed")],
                       lambda values: values + [values[-1]]):
            with self.subTest(modify=modify):
                with self.assertRaises(bundle.BundleError):
                    bundle.verify_bundle(self.mutated_archive(modify))
        raw = self.output.read_bytes()
        for cut in (len(raw) // 2, len(raw) - 512):
            damaged = self.root / "truncated.tar"
            damaged.write_bytes(raw[:cut])
            with self.assertRaises(bundle.BundleError):
                bundle.verify_bundle(damaged)

    def test_symlinks_and_path_traversal_are_rejected_without_extraction(self):
        self.write("quest/dist/link-target.js", b"asset")
        (self.repo / "quest/dist/link.js").symlink_to("link-target.js")
        with self.assertRaisesRegex(bundle.BundleError, "Symlink"):
            bundle.create_bundle(self.repo, self.output)
        (self.repo / "quest/dist/link.js").unlink()
        (self.repo / "quest/dist/link-target.js").unlink()
        bundle.create_bundle(self.repo, self.output)
        for name in ("../escape", "farsight/../../escape", "/absolute", "farsight\\escape"):
            with self.subTest(name=name), self.assertRaises(bundle.BundleError):
                bundle.verify_bundle(self.mutated_archive(lambda values: values + [(name, b"bad")]))
        self.assertFalse((self.root / "escape").exists())

    def test_model_and_committed_source_symlinks_are_rejected(self):
        self.models(["whisper-tiny"])
        model = self.repo / "models/ggml-tiny.en.bin"
        model.unlink()
        model.symlink_to(self.repo / "README.md")
        with self.assertRaisesRegex(bundle.BundleError, "Symlink"):
            bundle.create_bundle(self.repo, self.output, families=["whisper-tiny"])
        (self.repo / "source-link").symlink_to("README.md")
        self.commit()
        with self.assertRaisesRegex(bundle.BundleError, "Committed symlinks"):
            bundle.create_bundle(self.repo, self.output)

    def test_archive_symlink_and_gzip_truncation_never_extract_files(self):
        bundle.create_bundle(self.repo, self.output)
        entries = self.read_archive()
        malicious = self.root / "linked.tar"
        with tarfile.open(malicious, "w", format=tarfile.PAX_FORMAT) as archive:
            inventory = tarfile.TarInfo(bundle.MANIFEST)
            inventory.size = len(entries[0][1])
            archive.addfile(inventory, io.BytesIO(entries[0][1]))
            link = tarfile.TarInfo(entries[1][0])
            link.type, link.linkname = tarfile.SYMTYPE, "../../escape"
            archive.addfile(link)
        with self.assertRaises(bundle.BundleError):
            bundle.verify_bundle(malicious)
        self.assertFalse((self.root / "escape").exists())
        compressed = self.root / "compressed.tar.gz"
        bundle.create_bundle(self.repo, compressed, compression="gzip")
        compressed.write_bytes(compressed.read_bytes()[:-8])
        with self.assertRaises(bundle.BundleError):
            bundle.verify_bundle(compressed)

    def test_build_runs_known_npm_script_offline_without_install_then_marks_assets(self):
        (self.repo / "quest/dist" / bundle.BUILD_STAMP).unlink()
        original = subprocess.run
        commands = []
        def run(args, **options):
            if args[0] == "npm":
                commands.append((args, options))
                self.write("quest/dist/assets/view.js", b"rebuilt fixture")
                return subprocess.CompletedProcess(args, 0)
            return original(args, **options)
        with patch("tools.package_demo.subprocess.run", side_effect=run):
            report = bundle.create_bundle(self.repo, self.output, build=True)
        self.assertTrue(report["verified"])
        self.assertIn("--offline", commands[0][0])
        self.assertIn("--ignore-scripts", commands[0][0])
        self.assertNotIn("install", commands[0][0])
        self.assertEqual(commands[0][1]["env"]["npm_config_offline"], "true")

    def test_verifier_refuses_claims_of_hardware_or_dependency_qualification(self):
        bundle.create_bundle(self.repo, self.output)
        def claimed(entries):
            metadata = json.loads(entries[0][1])
            metadata["capabilities"]["hardware_qualified"] = True
            return [(bundle.MANIFEST, json.dumps(metadata).encode())] + entries[1:]
        with self.assertRaisesRegex(bundle.BundleError, "capability claims"):
            bundle.verify_bundle(self.mutated_archive(claimed))

    def test_verifier_checks_recognized_pair_even_when_member_is_dropped_from_inventory(self):
        self.models(["vision-smol"])
        bundle.create_bundle(self.repo, self.output, families=["vision-smol"])
        dropped = "farsight/models/" + bundle.MODEL_FAMILIES["vision-smol"]["files"][1]
        def remove(entries):
            metadata = json.loads(entries[0][1])
            metadata["files"] = [entry for entry in metadata["files"] if entry["path"] != dropped]
            return [(bundle.MANIFEST, json.dumps(metadata).encode())] + [entry for entry in entries[1:] if entry[0] != dropped]
        with self.assertRaisesRegex(bundle.BundleError, "family is incomplete"):
            bundle.verify_bundle(self.mutated_archive(remove))


if __name__ == "__main__":
    unittest.main()
