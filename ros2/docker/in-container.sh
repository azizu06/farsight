#!/usr/bin/env bash
# Runs inside farsight-nav:jazzy (see run-tests.sh). /farsight is the read-only repo, /out collects logs.
set -eo pipefail
source /opt/ros/jazzy/setup.bash
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
mkdir -p /ws/src
cp -r /farsight/ros2/farsight_nav /ws/src/
[ -d /farsight/ros2/farsight_slam ] && cp -r /farsight/ros2/farsight_slam /ws/src/ || true
(cd /ws && colcon build --packages-select farsight_nav >/out/colcon.log 2>&1) || { tail -40 /out/colcon.log; exit 1; }
source /ws/install/setup.bash
SUITE="${SUITE:-all}"

if [ "$SUITE" = unit ] || [ "$SUITE" = all ]; then
  echo "== unit"
  python3 -m pytest -q -p no:cacheprovider /farsight/ros2/farsight_nav/test
fi
if [ "$SUITE" = smoke ] || [ "$SUITE" = all ]; then
  echo "== smoke"
  python3 /farsight/ros2/tests/smoke.py
fi
if [ "$SUITE" = scenarios ] || [ "$SUITE" = all ]; then
  if [ -f /farsight/ros2/tests/scenarios.py ]; then
    echo "== scenarios"
    python3 /farsight/ros2/tests/scenarios.py "$@"
  fi
fi
