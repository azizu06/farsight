#!/usr/bin/env bash
# Install Isaac ROS nvblox (release 4.6, ROS 2 Jazzy) on the Jetson: the NVIDIA apt source plus ONLY the three packages
# the costmap needs (~346 MB), never the 3.2 GB ros-jazzy-isaac-ros-nvblox meta-package (Triton, cuVSLAM, UNet).
# Verifies Jetson Linux R39 and CUDA 13 first and simulates the install; it refuses if apt would remove or upgrade
# anything, or pull in CUDA/TensorRT/cuDNN packages that could clash with JetPack. Safe to re-run. Docs: docs/nvblox.md.
# Usage: bash jetson/nvblox-install.sh [--check] [--dry-run] [--no-build] [--allow-cuda-packages]
#   --check                preflight and the apt simulation only; adds no source or key and installs nothing
#   --dry-run              print every state-changing command instead of running it
#   --no-build             skip colcon build of ros2/farsight_nvblox
#   --allow-cuda-packages  accept new cuda-*/cudnn/tensorrt packages in the simulation (read the list first)
# Needs: ROS 2 Jazzy (jetson/ros2-install.sh) and internet for the apt source, key and packages. The board has no
# internet of its own: use a hotspot or Mac Internet Sharing, and do this before the venue. Never run it mid-demo.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RELEASE="${NVBLOX_ISAAC_RELEASE:-release-4.6}"       # pinned: release-4 floats and 5.0 is ROS 2 Lyrical only
SUITE="noble-jetpack"
REPO_URL="https://isaac.download.nvidia.com/isaac-ros"
KEYRING="${NVBLOX_KEYRING:-/usr/share/keyrings/nvidia-isaac-ros.gpg}"
SOURCES="${NVBLOX_SOURCES_LIST:-/etc/apt/sources.list.d/nvidia-isaac-ros.list}"
PIN="${NVBLOX_APT_PIN:-/etc/apt/preferences.d/nvidia-isaac-ros}"
TEGRA_RELEASE="${NVBLOX_TEGRA_RELEASE:-/etc/nv_tegra_release}"
ROS_SETUP="${NVBLOX_ROS_SETUP:-/opt/ros/jazzy/setup.bash}"
MIN_FREE_MB="${NVBLOX_MIN_FREE_MB:-3000}"
PACKAGES=(ros-jazzy-nvblox-ros ros-jazzy-nvblox-nav2 ros-jazzy-nvblox-msgs)
# Packages the Isaac repo must never replace: JetPack owns them and the Python/TensorRT stack depends on them.
PROTECTED='^(libopencv|opencv|python3-opencv|libnvinfer|python3-libnvinfer|tensorrt|nvidia-l4t|nvidia-jetpack|libcudnn|cuda|libcublas|libcusparse|libcusolver|libcufft|libnpp|libnccl)'
DRY=0 CHECK=0 BUILD=1 ALLOW_CUDA=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --dry-run) DRY=1 ;;
    --no-build) BUILD=0 ;;
    --allow-cuda-packages) ALLOW_CUDA=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

step() { printf '\n== %s\n' "$*"; }
die() { echo "nvblox-install: $*" >&2; exit 1; }
run() { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
# Dry-run and --check must not need sudo; real runs use it exactly like jetson/setup.sh.
SUDO=sudo; [ "$(id -u)" = 0 ] && SUDO=""

step "Preflight"
[ "$(uname -m)" = aarch64 ] || die "this is for the aarch64 Jetson, not $(uname -m)"
[ -r "$TEGRA_RELEASE" ] || die "$TEGRA_RELEASE missing: not a Jetson Linux install"
tegra="$(head -1 "$TEGRA_RELEASE")"
echo "Jetson Linux: $tegra"
case "$tegra" in
  "# R39"*) ;;
  *) die "need Jetson Linux R39 (JetPack 7.x); this reports '$tegra'. Isaac ROS 4.6 targets R39 only" ;;
esac
revision="$(printf '%s' "$tegra" | sed -n 's/.*REVISION: *\([0-9.]*\).*/\1/p')"
[ "$revision" = "2.0" ] || echo "note: NVIDIA documents R39 revision 2.0; this board has ${revision:-unknown}. A patch-level difference, unvalidated by NVIDIA."

nvcc_bin="$(command -v nvcc || true)"
[ -n "$nvcc_bin" ] || [ ! -x /usr/local/cuda/bin/nvcc ] || nvcc_bin=/usr/local/cuda/bin/nvcc
cuda=""
if [ -n "$nvcc_bin" ]; then cuda="$("$nvcc_bin" --version 2>/dev/null | sed -n 's/.*release \([0-9][0-9.]*\).*/\1/p' | head -1)"; fi
if [ -z "$cuda" ]; then
  cuda="$(dpkg-query -W -f='${Package} ${Version}\n' 2>/dev/null | sed -n 's/^cuda-toolkit-\([0-9]*\)-\([0-9]*\) .*/\1.\2/p' | sort -V | tail -1)"
fi
[ -n "$cuda" ] || die "no CUDA toolkit found (nvcc or cuda-toolkit-* package); finish jetson/setup.sh first"
echo "CUDA toolkit: $cuda"
[ "${cuda%%.*}" = 13 ] || die "need CUDA 13.x (Isaac ROS 4.6 on JetPack 7.2); found $cuda"

[ -f "$ROS_SETUP" ] || die "ROS 2 Jazzy not found at $ROS_SETUP; run bash jetson/ros2-install.sh first"
free_mb="$(df -Pm / | awk 'NR==2 {print $4}')"
echo "Free disk on /: ${free_mb} MB (need ${MIN_FREE_MB} MB for 346 MB of packages plus their dependencies)"
[ "$free_mb" -ge "$MIN_FREE_MB" ] || die "only ${free_mb} MB free on /; need ${MIN_FREE_MB} MB"
echo "RAM now: $(free -m 2>/dev/null | awk '/Mem:/ {print $7 " MB available of " $2 " MB"}')"
echo "Packages: ${PACKAGES[*]}"

step "NVIDIA Isaac ROS apt source ($RELEASE, $SUITE)"
if [ "$CHECK" = 1 ] && { [ ! -f "$SOURCES" ] || [ ! -s "$KEYRING" ]; }; then
  echo "--check: not adding the apt source or key; showing what a real run does:"
  echo "  key     $REPO_URL/repos.key -> $KEYRING"
  echo "  source  deb [arch=arm64 signed-by=$KEYRING] $REPO_URL/$RELEASE $SUITE main -> $SOURCES"
  echo "  pin     origin isaac.download.nvidia.com at priority 1 -> $PIN (never replaces JetPack/Ubuntu packages)"
  echo "(apt simulation needs the source; rerun without --check to add it, or add it by hand first)"
  exit 0
fi
if [ ! -s "$KEYRING" ]; then
  run $SUDO apt-get update
  run $SUDO apt-get install -y curl gnupg ca-certificates
  if [ "$DRY" = 1 ]; then
    printf '  [dry-run] curl -fsSL %s/repos.key | gpg --dearmor | tee %s\n' "$REPO_URL" "$KEYRING"
  else
    curl -fsSL "$REPO_URL/repos.key" | gpg --dearmor | $SUDO tee "$KEYRING" >/dev/null
    [ -s "$KEYRING" ] || die "could not fetch the NVIDIA Isaac ROS key from $REPO_URL/repos.key (internet?)"
  fi
else
  echo "keyring already present: $KEYRING"
fi
source_line="deb [arch=arm64 signed-by=$KEYRING] $REPO_URL/$RELEASE $SUITE main"
if ! grep -qsF "$source_line" "$SOURCES"; then
  if [ "$DRY" = 1 ]; then printf '  [dry-run] write %s: %s\n' "$SOURCES" "$source_line"; else printf '%s\n' "$source_line" | $SUDO tee "$SOURCES" >/dev/null; fi
fi
# Priority 1: this repo is used only for packages no other repo provides, so it can never swap JetPack's OpenCV,
# TensorRT or CUDA for NVIDIA's container-flavoured builds. A dependency it cannot satisfy fails the simulation below.
pin_text=$'Package: *\nPin: origin isaac.download.nvidia.com\nPin-Priority: 1\n'
if [ "$(cat "$PIN" 2>/dev/null || true)" != "${pin_text%$'\n'}" ]; then
  if [ "$DRY" = 1 ]; then printf '  [dry-run] write %s:\n%s' "$PIN" "$pin_text"; else printf '%s' "$pin_text" | $SUDO tee "$PIN" >/dev/null; fi
fi
run $SUDO apt-get update

step "Simulate the install (nothing is changed)"
simulation="$(mktemp "${TMPDIR:-/tmp}/nvblox-apt-sim.XXXXXX")"
trap 'rm -f "$simulation"' EXIT
if [ "$DRY" = 1 ]; then
  printf '  [dry-run] apt-get -s install --no-install-recommends %s\n' "${PACKAGES[*]}"
else
  apt-get -s install --no-install-recommends "${PACKAGES[@]}" >"$simulation" 2>&1 || { cat "$simulation"; die "apt cannot resolve ${PACKAGES[*]} (see above); no nvblox package was installed"; }
  new_count="$(grep -c '^Inst ' "$simulation" || true)"
  echo "apt would install or upgrade $new_count package(s)"
  removals="$(grep '^Remv ' "$simulation" || true)"
  [ -z "$removals" ] || { printf '%s\n' "$removals"; die "apt would REMOVE packages (above); not installing. No nvblox package was installed."; }
  upgrades="$(grep -E '^Inst [^ ]+ \[' "$simulation" | awk '{print $2}' | grep -E "$PROTECTED" || true)"
  [ -z "$upgrades" ] || { printf 'would upgrade:\n%s\n' "$upgrades"; die "apt would UPGRADE JetPack-owned packages (above); not installing. No nvblox package was installed."; }
  clash="$(grep -E '^Inst [^ ]+ \(' "$simulation" | awk '{print $2}' | grep -E "$PROTECTED" || true)"
  if [ -n "$clash" ]; then
    printf 'would newly install:\n%s\n' "$clash"
    if [ "$ALLOW_CUDA" = 0 ]; then
      die "apt wants CUDA/TensorRT/cuDNN/OpenCV packages (above), which can clash with JetPack's ($cuda). Review them; rerun with --allow-cuda-packages only if they are additive. No nvblox package was installed."
    fi
    echo "--allow-cuda-packages given: continuing"
  fi
  echo "simulation clean: no removals, no JetPack-owned upgrades"
fi
[ "$CHECK" = 1 ] && { echo "--check: stopping before the real install"; exit 0; }

step "Install"
run $SUDO apt-get install -y --no-install-recommends "${PACKAGES[@]}"

step "Verify"
if [ "$DRY" = 1 ]; then
  echo "  [dry-run] dpkg-query the three packages; ros2 pkg prefix nvblox_ros nvblox_nav2 nvblox_msgs; ldd the plugin library"
else
  dpkg-query -W -f='${Package} ${Version}\n' "${PACKAGES[@]}"
  set +u
  # shellcheck disable=SC1090
  . "$ROS_SETUP"
  set -u
  for package in nvblox_ros nvblox_nav2 nvblox_msgs; do
    prefix="$(ros2 pkg prefix "$package" 2>/dev/null)" || die "ros2 cannot find $package after the install"
    echo "$package -> $prefix"
  done
  missing_libs="$(find "$(ros2 pkg prefix nvblox_ros)/lib" -maxdepth 2 -name '*nvblox*.so*' -exec ldd {} + 2>/dev/null | grep 'not found' | sort -u || true)"
  [ -z "$missing_libs" ] || { printf '%s\n' "$missing_libs"; die "nvblox libraries have unresolved shared libraries (above); do not proceed to the smoke test"; }
  echo "nvblox shared libraries resolve"
fi

if [ "$BUILD" = 1 ] && [ -d "$REPO/ros2/farsight_nvblox" ]; then
  step "Build ros2/farsight_nvblox (offline)"
  run bash -c "cd '$REPO/ros2' && source '$ROS_SETUP' && colcon build --symlink-install --packages-select farsight_nvblox"
fi

step "Next"
echo "1. Measure first: bash jetson/nvblox-smoke.sh --source synthetic   # one-hour go/no-go, report file with PASS/FAIL"
echo "2. Only on PASS plus the live-phone gates in docs/nvblox.md: costmap_source:=nvblox. Default stays pointcloud."
echo "3. Rollback: sudo apt-get remove ros-jazzy-nvblox-ros ros-jazzy-nvblox-nav2 ros-jazzy-nvblox-msgs && sudo apt-get autoremove --dry-run"
echo "   then delete $SOURCES, $KEYRING and $PIN."
