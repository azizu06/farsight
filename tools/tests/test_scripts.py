"""Bring-up shell scripts: syntax, shellcheck when installed, dry runs, and a real certificate round trip."""

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ["jetson/setup.sh", "jetson/install_service.sh", "jetson/make_cert.sh",
           "tools/bootstrap_jetson.sh", "tools/push_models.sh"]


def sh(*args, env=None):
    return subprocess.run(["bash", *args], cwd=ROOT, capture_output=True, text=True, timeout=60,
                          env={**os.environ, **(env or {})})


@unittest.skipUnless(shutil.which("bash"), "bash required")
class ScriptTests(unittest.TestCase):
    def test_syntax(self):
        for script in SCRIPTS:
            result = sh("-n", script)
            self.assertEqual(result.returncode, 0, f"{script}: {result.stderr}")

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed (pip install shellcheck-py)")
    def test_shellcheck_clean(self):
        result = subprocess.run(["shellcheck", *SCRIPTS], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_setup_dry_run_lists_every_stage_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            result = sh("jetson/setup.sh", "--dry-run", "--skip-builds", env={"FARSIGHT_VENV": f"{directory}/venv"})
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ("apt-get install -y", "nvidia-jetpack", "venv --system-site-packages", "dialout",
                         "would download", "es_MX-claude-high", "version-RFB-320", "yolo11n"):
            self.assertIn(expected, result.stdout.replace("[dry-run] ", ""), expected)
        self.assertNotIn("password", result.stdout.lower())

    def test_setup_dry_run_builds_for_orin_sm87(self):
        result = sh("jetson/setup.sh", "--dry-run")
        self.assertIn("-DCMAKE_CUDA_ARCHITECTURES=87", result.stdout)
        self.assertIn("--target llama-server", result.stdout)
        self.assertIn("--target whisper-server whisper-cli", result.stdout)

    def test_unknown_option_is_rejected(self):
        for script in ("jetson/setup.sh", "jetson/install_service.sh", "tools/push_models.sh", "tools/bootstrap_jetson.sh"):
            result = sh(script, "--nope")
            self.assertEqual(result.returncode, 2, script)

    def test_bootstrap_requires_a_target_and_never_touches_ssh_on_dry_run(self):
        self.assertEqual(sh("tools/bootstrap_jetson.sh").returncode, 2)
        result = sh("tools/bootstrap_jetson.sh", "--dry-run", "--no-models", "me@192.168.55.1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("jetson/setup.sh", result.stdout)
        self.assertIn("tmux new-session -A -s setup", result.stdout)
        self.assertNotIn("BatchMode=yes me@192.168.55.1 true\n", result.stderr)

    def test_install_service_dry_run_renders_env_and_checks_it(self):
        result = sh("jetson/install_service.sh", "--dry-run", "--no-tls")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("FARSIGHT_DETECTOR=", result.stdout)
        self.assertIn("jetson.launch --check --env-file /etc/farsight.env", result.stdout)
        self.assertIn("systemctl restart farsight.service", result.stdout)

    def test_units_use_the_canonical_entry_point_and_templates_are_complete(self):
        service = (ROOT / "jetson/farsight.service").read_text()
        self.assertIn("-m jetson.launch", service)
        for unit in ("farsight.service", "farsight-llm.service", "farsight-whisper.service"):
            text = (ROOT / "jetson" / unit).read_text()
            self.assertNotIn("jetson/farsight/", text)
            self.assertTrue(all(token in ("@USER@", "@REPO@", "@SRC@") for token in
                                {t for t in text.replace("#", " ").split() if t.startswith("@") and t.endswith("@")}), unit)

    def test_no_password_or_secret_literals_in_scripts(self):
        for script in SCRIPTS + ["jetson/farsight.env.example"]:
            text = (ROOT / script).read_text().lower()
            for bad in ("password=", "passwd=", "sudo -s", "echo $password", "| sudo -s"):
                self.assertNotIn(bad, text, f"{script}: {bad}")


@unittest.skipUnless(shutil.which("openssl") and shutil.which("bash"), "openssl required")
class CertTests(unittest.TestCase):
    def test_certificate_verifies_by_name_and_ip_with_the_private_ca_only(self):
        with tempfile.TemporaryDirectory() as directory:
            result = sh("jetson/make_cert.sh", "--dir", directory, "rover.example", "10.9.8.7")
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("ca.pem", "cert.pem", "key.pem", "ca-key.pem"):
                self.assertTrue((Path(directory) / name).stat().st_size > 0, name)
            self.assertEqual((Path(directory) / "key.pem").stat().st_mode & 0o077, 0, "server key must be private")
            first = (Path(directory) / "cert.pem").read_bytes()
            self.assertIn("kept existing", sh("jetson/make_cert.sh", "--dir", directory).stdout)
            self.assertEqual((Path(directory) / "cert.pem").read_bytes(), first, "re-run must not replace the certificate")

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")

                def log_message(self, *args):
                    pass

            server = HTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(Path(directory) / "cert.pem", Path(directory) / "key.pem")
            server.socket = context.wrap_socket(server.socket, server_side=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.shutdown)
            client = ssl.create_default_context(cafile=str(Path(directory) / "ca.pem"))
            for host in ("127.0.0.1", "localhost"):
                with urllib.request.urlopen(f"https://{host}:{server.server_port}/", context=client, timeout=5) as response:
                    self.assertEqual(response.read(), b"ok")
            with self.assertRaises(OSError):  # the system trust store does not know this CA
                urllib.request.urlopen(f"https://127.0.0.1:{server.server_port}/", timeout=5)
            self.assertIn("rover.example", sh("jetson/make_cert.sh", "--dry-run", "--dir", directory, "rover.example").stdout)


if __name__ == "__main__":
    unittest.main()
