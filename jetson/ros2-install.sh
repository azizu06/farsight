#!/usr/bin/env bash
# Install ROS 2 Jazzy + Nav2 on the Jetson (Ubuntu 24.04 Noble, arm64), build ros2/farsight_nav, and install
# the farsight-nav systemd unit (disabled unless --enable). Navigation stays opt-in: jetson.server only talks
# to it when started with --nav2 (FARSIGHT_NAV2=1). Safe to re-run. Docs: docs/navigation.md.
# Usage: bash jetson/ros2-install.sh [--dry-run] [--check] [--no-build] [--enable] [--with-slam]
#   --check     print OS, disk, RAM and the exact apt list, change nothing (no internet needed)
#   --dry-run   print every command instead of running it
#   --with-slam also install SLAM Toolbox (ros2/farsight_slam)
# Needs internet for: the ROS apt source (.deb from github.com/ros-infrastructure/ros-apt-source) and
# packages.ros.org / ports.ubuntu.com downloads. Everything after the apt step works offline.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-${USER:-$(id -un)}}"
DRY=0 CHECK=0 BUILD=1 ENABLE=0 SLAM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --check) CHECK=1 ;;
    --no-build) BUILD=0 ;;
    --enable) ENABLE=1 ;;
    --with-slam) SLAM=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done

# Exact package list (Jazzy, Noble arm64). navigation2 pulls the Smac planners, MPPI and RPP controllers,
# costmap_2d (obstacle/voxel/inflation/range layers), behavior_server, bt_navigator and lifecycle manager.
APT_PACKAGES=(
  ros-jazzy-ros-base
  ros-jazzy-navigation2
  ros-jazzy-nav2-bringup
  ros-jazzy-pointcloud-to-laserscan
  ros-jazzy-tf2-ros
  ros-jazzy-tf-transformations
  ros-jazzy-rmw-cyclonedds-cpp
  python3-colcon-common-extensions
  python3-rosdep
  python3-websockets
  python3-numpy
)
SLAM_PACKAGES=(ros-jazzy-slam-toolbox)  # docs/navigation.md: no EKF (robot_localization) needed
if [ "$SLAM" = 1 ]; then APT_PACKAGES+=("${SLAM_PACKAGES[@]}"); fi

step() { printf '\n== %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

step "Target"
. /etc/os-release 2>/dev/null || true
echo "OS: ${PRETTY_NAME:-unknown} (${VERSION_CODENAME:-?}), arch $(uname -m)"
echo "Disk free on /: $(df -h / | awk 'NR==2 {print $4}') (measured on a minimal Noble arm64 container: 630 MB download, 3.2 GB installed; less when desktop/dev packages exist)"
echo "RAM: $(free -h 2>/dev/null | awk '/Mem:/ {print $2 " total, " $7 " available"}')"
echo "Packages: ${APT_PACKAGES[*]}"
if [ "${VERSION_CODENAME:-}" != noble ]; then
  echo "WARNING: ROS 2 Jazzy binaries target Ubuntu 24.04 noble; this host reports '${VERSION_CODENAME:-unknown}'." >&2
  [ "$CHECK" = 1 ] || [ "$DRY" = 1 ] || exit 1
fi
if [ "$CHECK" = 1 ]; then
  if [ -f /opt/ros/jazzy/setup.bash ]; then echo "ROS 2 Jazzy already installed at /opt/ros/jazzy"; fi
  dpkg-query -W -f='${Package} ${Version}\n' "${APT_PACKAGES[@]}" 2>/dev/null || true
  exit 0
fi

step "ROS 2 apt source (internet)"
if ! dpkg -s ros2-apt-source >/dev/null 2>&1 && [ ! -f /etc/apt/sources.list.d/ros2.list ] && [ ! -f /etc/apt/sources.list.d/ros2.sources ]; then
  run sudo apt-get update
  run sudo apt-get install -y curl ca-certificates software-properties-common
  run sudo add-apt-repository -y universe
  version="$(curl -fsSL https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | grep -F '"tag_name"' | awk -F'"' '{print $4}')" || version=""
  if [ -z "$version" ] && [ "$DRY" = 0 ]; then echo "Could not reach GitHub for ros-apt-source; check internet" >&2; exit 1; fi
  deb="/tmp/ros2-apt-source.deb"
  run curl -fL -o "$deb" "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${version:-VERSION}/ros2-apt-source_${version:-VERSION}.noble_all.deb"
  run sudo dpkg -i "$deb"
else
  echo "ROS 2 apt source already configured"
fi

step "Install packages (internet)"
run sudo apt-get update
run sudo apt-get install -y --no-install-recommends "${APT_PACKAGES[@]}"

if [ "$BUILD" = 1 ]; then
  step "Build ros2/farsight_nav (offline)"
  build="cd '$REPO/ros2' && source /opt/ros/jazzy/setup.bash && colcon build --symlink-install --packages-select farsight_nav"
  if [ "$SLAM" = 1 ] && [ -d "$REPO/ros2/farsight_slam" ]; then
    build="cd '$REPO/ros2' && source /opt/ros/jazzy/setup.bash && colcon build --symlink-install --packages-select farsight_nav farsight_slam"
  fi
  run bash -c "$build"
fi

step "systemd unit farsight-nav (installed, $( [ "$ENABLE" = 1 ] && echo enabled || echo 'not enabled; pass --enable' ))"
unit="$(sed -e "s#@USER@#$RUN_USER#g" -e "s#@REPO@#$REPO#g" "$REPO/jetson/farsight-nav.service")"
if [ "$DRY" = 1 ]; then
  printf '  [dry-run] write /etc/systemd/system/farsight-nav.service:\n%s\n' "$unit"
else
  printf '%s\n' "$unit" | sudo tee /etc/systemd/system/farsight-nav.service >/dev/null
fi
if [ -d /run/systemd/system ] || [ "$DRY" = 1 ]; then
  run sudo systemctl daemon-reload
  if [ "$ENABLE" = 1 ]; then
    run sudo systemctl enable --now farsight-nav.service
  fi
else
  echo "systemd is not running here (container?); unit written but not loaded"
fi

step "Next"
echo "1. Start the rover server with navigation: add FARSIGHT_NAV2=1 to /etc/farsight.env (or pass --nav2), restart farsight."
echo "2. sudo systemctl start farsight-nav   # journalctl -u farsight-nav -f"
echo "3. curl -s http://127.0.0.1:8443/nav/status   # backend should read nav2 once the bridge connects"
