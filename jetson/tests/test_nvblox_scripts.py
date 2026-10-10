"""Run jetson/nvblox-smoke.sh and jetson/nvblox-install.sh against fake ros2/tegrastats/apt-get binaries.

Nothing here touches ROS, CUDA or apt: each test builds a directory of tiny shell fakes, puts it first on PATH and
checks what the real script decides. They prove the scripts' branching and verdicts, not that nvblox runs.
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SMOKE = REPO / "jetson" / "nvblox-smoke.sh"
INSTALL = REPO / "jetson" / "nvblox-install.sh"
BASH = "/bin/bash"


def write_executable(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


class FakeBin:
    """A PATH directory of fake commands plus a scratch directory they read canned data from."""

    def __init__(self, test):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()

    def command(self, name, body):
        write_executable(self.bin / name, body)

    def env(self, **extra):
        env = dict(os.environ, PATH=f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin", HOME=str(self.root), TMPDIR=str(self.root))
        env.update({k: str(v) for k, v in extra.items()})
        return env


class SmokeScriptTests(unittest.TestCase):
    def fakes(self, *, hz="9.9", available_kb=3000000, up=True, gpu=40, delay="0.120", packages=True, cuda_error=False):
        fb = FakeBin(self)
        state = fb.root / "state"
        state.mkdir()
        fb.command("ros2", f'''
S="{state}"
case "$1 $2" in
  "pkg prefix") {'exit 0' if packages else '[ "$3" = nvblox_ros ] && exit 1; exit 0'} ;;
  "launch "*) {'touch "$S/up"; ' if up else ''}{'echo "CUDA error: no kernel image" ;' if cuda_error else ''} trap 'exit 0' INT TERM; while true; do sleep 1; done ;;
  "node list") [ -f "$S/up" ] && echo /nvblox_node ;;
  "param list") sed 's/^/  /' "$S/declared" ;;
  "topic hz") echo "average rate: {hz}"; exec sleep 100 ;;
  "topic delay") echo "average delay: {delay}"; echo "	min: 0.05 max: 0.2"; exec sleep 100 ;;
  *) exit 0 ;;
esac
''')
        fb.command("tegrastats", f'''while true; do echo "10-10-2026 10:00:00 RAM 4800/7620MB (lfb 4x2MB) SWAP 0/3810MB (cached 0MB) GR3D_FREQ {gpu}% VDD_IN 7000mW/7000mW"; sleep 1; done''')
        fb.command("pgrep", "exit 1")
        fb.command("nvcc", 'echo "Cuda compilation tools, release 13.0, V13.0.48"')
        fb.command("nvpmodel", 'echo "NV Power Mode: 15W"')
        meminfo = fb.root / "meminfo"
        meminfo.write_text(f"MemTotal: 7800000 kB\nMemAvailable: {available_kb} kB\n")
        import yaml
        params = yaml.safe_load((REPO / "ros2/farsight_nvblox/config/nvblox.yaml").read_text())["/**"]["ros__parameters"]
        from jetson import nvblox_smoke
        (state / "declared").write_text("\n".join(nvblox_smoke.flatten_param_keys({"/**": {"ros__parameters": params}})) + "\n")
        return fb, state, meminfo

    def run_script(self, fb, meminfo, *args):
        report = fb.root / "report.md"
        env = fb.env(NVBLOX_ROS_SETUP="/nonexistent", NVBLOX_WS_SETUP="/nonexistent", NVBLOX_MEMINFO=meminfo,
                     PYTHONPATH=REPO, PYTHON=sys.executable)
        result = subprocess.run([BASH, str(SMOKE), "--duration", "5", "--warmup", "0", "--report", str(report), *args],
                                capture_output=True, text=True, env=env, cwd=REPO, timeout=120)
        return result, report

    def test_healthy_synthetic_run_passes_and_writes_a_report(self):
        fb, _, meminfo = self.fakes()
        result, report = self.run_script(fb, meminfo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        text = report.read_text()
        self.assertIn("nvblox smoke test: PASS", text)
        self.assertIn("| esdf slice rate | PASS |", text)
        self.assertIn("| parameters | PASS | all", text)
        self.assertIn("power mode: NV Power Mode: 15W", text)
        self.assertIn("cuda: 13.0", text)

    def test_low_memory_fails_the_run(self):
        fb, _, meminfo = self.fakes(available_kb=600000)
        result, report = self.run_script(fb, meminfo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("| memory | FAIL |", report.read_text())

    def test_slow_slice_fails_the_run(self):
        fb, _, meminfo = self.fakes(hz="1.5")
        result, report = self.run_script(fb, meminfo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("| esdf slice rate | FAIL |", report.read_text())

    def test_cuda_error_in_the_launch_log_fails_the_run(self):
        fb, _, meminfo = self.fakes(cuda_error=True)
        result, report = self.run_script(fb, meminfo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("| cuda errors | FAIL |", report.read_text())

    def test_undeclared_parameter_fails_the_run(self):
        fb, state, meminfo = self.fakes()
        declared = (state / "declared").read_text().replace("static_mapper.esdf_slice_min_height\n", "")
        (state / "declared").write_text(declared)
        result, report = self.run_script(fb, meminfo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("static_mapper.esdf_slice_min_height", report.read_text())

    def test_node_that_never_starts_is_a_fail_with_a_report(self):
        fb, _, meminfo = self.fakes(up=False)
        # shorten the 90 s start-up wait: the fake launch exits immediately, which the script treats as a dead launch
        fb.command("ros2", (fb.bin / "ros2").read_text().split("\n", 1)[1].replace(
            'trap \'exit 0\' INT TERM; while true; do sleep 1; done', 'exit 3'))
        result, report = self.run_script(fb, meminfo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("| startup | FAIL |", report.read_text())

    def test_missing_nvblox_package_stops_before_launching(self):
        fb, state, meminfo = self.fakes(packages=False)
        result, _ = self.run_script(fb, meminfo)
        self.assertEqual(result.returncode, 2)
        self.assertIn("ROS package nvblox_ros missing", result.stderr)
        self.assertFalse((state / "up").exists())

    def test_check_and_dry_run_launch_nothing(self):
        fb, state, meminfo = self.fakes()
        result, _ = self.run_script(fb, meminfo, "--check")
        self.assertEqual((result.returncode, "preflight OK" in result.stdout), (0, True))
        result, _ = self.run_script(fb, meminfo, "--dry-run")
        self.assertEqual(result.returncode, 0)
        self.assertIn("synthetic:=true", result.stdout)
        self.assertFalse((state / "up").exists())

    def test_bad_arguments_exit_2(self):
        fb, _, meminfo = self.fakes()
        for args in (["--source", "bag"], ["--source", "nope"], ["--duration", "abc"]):
            result, _ = self.run_script(fb, meminfo, *args)
            self.assertEqual(result.returncode, 2, args)


if __name__ == "__main__":
    unittest.main()


class InstallScriptTests(unittest.TestCase):
    CLEAN_SIM = "Inst ros-jazzy-nvblox-msgs (4.6.0 Isaac:noble [arm64])\nInst ros-jazzy-nvblox-ros (4.6.0 Isaac:noble [arm64])\nInst ros-jazzy-nvblox-nav2 (4.6.0 Isaac:noble [arm64])\n"

    def setup_rig(self, *, tegra="# R39 (release), REVISION: 2.1, GCID: 1, BOARD: generic", cuda="13.0", simulation=None,
                  ros=True, machine="aarch64", ldd=""):
        fb = FakeBin(self)
        r = fb.root
        (r / "nv_tegra_release").write_text(tegra + "\n")
        (r / "sim").write_text(self.CLEAN_SIM if simulation is None else simulation)
        (r / "prefix" / "lib").mkdir(parents=True)
        (r / "prefix" / "lib" / "libnvblox_ros_test.so").write_text("x")
        if ros:
            (r / "setup.bash").write_text("# fake ROS setup\n")
        fb.command("uname", f'[ "$1" = -m ] && echo {machine} || /usr/bin/uname "$@"')
        fb.command("sudo", 'exec "$@"')
        fb.command("curl", 'echo FAKEKEY')
        fb.command("gpg", 'cat')
        fb.command("free", 'echo "              total        used        free      shared  buff/cache   available"; echo "Mem:           7620        3000        2000         100        2600        4300"')
        fb.command("nvcc", f'echo "Cuda compilation tools, release {cuda}, V{cuda}.48"')
        fb.command("dpkg-query", 'echo "$@" | grep -q "cuda-toolkit" && exit 0; for p in "${@:3}"; do echo "$p 4.6.0"; done')
        fb.command("ros2", f'[ "$1 $2" = "pkg prefix" ] && echo "{r / "prefix"}"')
        fb.command("ldd", f'printf %s "{ldd}"')
        fb.command("apt-get", f'''echo "apt-get $*" >> "{r}/apt.log"
case "$*" in
  *"-s install"*) cat "{r}/sim" ;;
esac
exit 0''')
        return fb

    def run_install(self, fb, *args):
        r = fb.root
        env = fb.env(NVBLOX_TEGRA_RELEASE=r / "nv_tegra_release", NVBLOX_ROS_SETUP=r / "setup.bash",
                     NVBLOX_KEYRING=r / "keyring.gpg", NVBLOX_SOURCES_LIST=r / "isaac.list", NVBLOX_APT_PIN=r / "pin",
                     NVBLOX_MIN_FREE_MB="1")
        return subprocess.run([BASH, str(INSTALL), *args], capture_output=True, text=True, env=env, cwd=REPO, timeout=60)

    def apt_log(self, fb):
        path = fb.root / "apt.log"
        return path.read_text() if path.exists() else ""

    def test_clean_install_writes_the_pinned_source_and_installs_only_the_three_packages(self):
        fb = self.setup_rig()
        result = self.run_install(fb, "--no-build")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        source = (fb.root / "isaac.list").read_text()
        self.assertIn("isaac-ros/release-4.6 noble-jetpack main", source)
        self.assertIn(f"signed-by={fb.root / 'keyring.gpg'}", source)
        self.assertIn("Pin-Priority: 1", (fb.root / "pin").read_text())
        self.assertEqual((fb.root / "keyring.gpg").read_text().strip(), "FAKEKEY")
        installs = [line for line in self.apt_log(fb).splitlines() if "install -y" in line]
        self.assertEqual(installs[-1], "apt-get install -y --no-install-recommends "
                         "ros-jazzy-nvblox-ros ros-jazzy-nvblox-nav2 ros-jazzy-nvblox-msgs")
        self.assertNotIn("ros-jazzy-isaac-ros-nvblox", self.apt_log(fb))

    def test_second_run_does_not_duplicate_the_source(self):
        fb = self.setup_rig()
        self.run_install(fb, "--no-build")
        self.run_install(fb, "--no-build")
        self.assertEqual((fb.root / "isaac.list").read_text().count("deb "), 1)

    def test_wrong_jetson_release_cuda_or_arch_stops_before_apt(self):
        for label, kwargs, message in (
                ("r36", dict(tegra="# R36 (release), REVISION: 4.0"), "need Jetson Linux R39"),
                ("cuda12", dict(cuda="12.6"), "need CUDA 13.x"),
                ("no ros", dict(ros=False), "ROS 2 Jazzy not found"),
                ("x86", dict(machine="x86_64"), "aarch64 Jetson")):
            fb = self.setup_rig(**kwargs)
            result = self.run_install(fb, "--no-build")
            self.assertEqual(result.returncode, 1, label)
            self.assertIn(message, result.stderr, label)
            self.assertEqual(self.apt_log(fb), "", label)

    def test_removals_or_jetpack_upgrades_in_the_simulation_block_the_install(self):
        cases = (("removal", "Remv libopencv-core 4.8\n" + self.CLEAN_SIM, "would REMOVE"),
                 ("upgrade", "Inst libnvinfer10 [10.3] (10.16 Isaac:noble [arm64])\n" + self.CLEAN_SIM, "would UPGRADE"))
        for label, sim, message in cases:
            fb = self.setup_rig(simulation=sim)
            result = self.run_install(fb, "--no-build")
            self.assertEqual(result.returncode, 1, label)
            self.assertIn(message, result.stderr, label)
            self.assertNotIn("install -y --no-install-recommends", self.apt_log(fb), label)

    def test_new_cuda_packages_need_an_explicit_flag(self):
        sim = "Inst cuda-toolkit-13-2 (13.2 Nvidia [arm64])\n" + self.CLEAN_SIM
        fb = self.setup_rig(simulation=sim)
        result = self.run_install(fb, "--no-build")
        self.assertEqual(result.returncode, 1)
        self.assertIn("clash with JetPack", result.stderr)
        self.assertNotIn("install -y --no-install-recommends", self.apt_log(fb))
        result = self.run_install(fb, "--no-build", "--allow-cuda-packages")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unresolved_shared_libraries_fail_the_verification(self):
        fb = self.setup_rig(ldd="libcudart.so.13 => not found\n")
        result = self.run_install(fb, "--no-build")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unresolved shared libraries", result.stderr)

    def test_check_installs_nothing_and_writes_nothing(self):
        fb = self.setup_rig()
        result = self.run_install(fb, "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((fb.root / "isaac.list").exists() or (fb.root / "keyring.gpg").exists())
        self.assertEqual(self.apt_log(fb), "")
        # With the source already present --check simulates but still never installs.
        (fb.root / "isaac.list").write_text("deb x\n")
        (fb.root / "keyring.gpg").write_text("k")
        result = self.run_install(fb, "--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("simulation clean", result.stdout)
        self.assertNotIn("install -y --no-install-recommends", self.apt_log(fb))

    def test_dry_run_changes_nothing(self):
        fb = self.setup_rig()
        result = self.run_install(fb, "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[dry-run]", result.stdout)
        self.assertEqual(self.apt_log(fb), "")
        self.assertFalse((fb.root / "isaac.list").exists())
