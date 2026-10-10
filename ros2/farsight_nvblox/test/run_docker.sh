#!/usr/bin/env bash
# Build the test image, colcon-build farsight_nvblox in it and run the tests, launch --show-args and a synthetic
# depth/TF probe. The container has no network so other ROS containers on this machine cannot leak into the run.
#   ros2/farsight_nvblox/test/run_docker.sh
set -euo pipefail
pkg="$(cd "$(dirname "$0")/.." && pwd)"
docker build -q -t farsight-nvblox:jazzy -f "$pkg/docker/Dockerfile" "$pkg/docker" >/dev/null
docker run --rm --network none -e ROS_DOMAIN_ID=77 -v "$pkg:/ws/src/farsight_nvblox:ro" -w /ws farsight-nvblox:jazzy bash -c '
  set -e
  source /opt/ros/jazzy/setup.bash
  colcon build --packages-select farsight_nvblox --build-base /tmp/build --install-base /tmp/install >/tmp/colcon.log 2>&1 || { cat /tmp/colcon.log; exit 1; }
  source /tmp/install/setup.bash
  cd src/farsight_nvblox && python3 -m pytest -q -p no:cacheprovider test
  echo "== launch arguments"
  ros2 launch farsight_nvblox nvblox.launch.py --show-args | grep -c "^    .*:$"
  echo "== synthetic depth + TF"
  ros2 run farsight_nvblox synthetic_depth >/tmp/synthetic.log 2>&1 &
  sleep 3
  python3 test/synthetic_probe.py
'
