#!/usr/bin/env bash
# Build the SLAM test image, colcon-build farsight_slam inside it and run the tests.
#   ros2/farsight_slam/test/run_docker.sh            # unit + loop-closure tests
#   ros2/farsight_slam/test/run_docker.sh -k world   # pytest selection
# Loop-closure reports land in ros2/farsight_slam/test/results/.
set -euo pipefail
pkg="$(cd "$(dirname "$0")/.." && pwd)"
docker build -q -t farsight-slam:jazzy -f "$pkg/docker/Dockerfile" "$pkg/docker" >/dev/null
docker run --rm --network none -v "$pkg:/ws/src/farsight_slam" -w /ws farsight-slam:jazzy bash -c '
  set -e
  source /opt/ros/jazzy/setup.bash
  colcon build --packages-select farsight_slam --symlink-install >/tmp/colcon.log 2>&1 || { cat /tmp/colcon.log; exit 1; }
  source install/setup.bash
  cd src/farsight_slam && python3 -m pytest -v -p no:cacheprovider test "$@"
' -- "$@"
