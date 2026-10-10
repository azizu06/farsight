#!/usr/bin/env bash
# Build ros2/farsight_nav in the farsight-nav:jazzy container (ROS 2 Jazzy + Nav2) and run its tests.
# Usage: bash ros2/docker/run-tests.sh [unit|smoke|scenarios|all] [extra args for the scenario runner]
#   unit       pure frame/protocol tests (no ROS graph)
#   smoke      jetson.server --demo --nav2 + nav.launch.py: bridge connects, Nav2 lifecycle comes up
#   scenarios  closed-loop simulator worlds driven through Nav2 (ros2/tests/scenarios.py)
# The repo is mounted read-only; colcon builds inside the container. Docker Desktop must be running.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SUITE="${1:-all}"
shift || true
IMAGE="${FARSIGHT_NAV_IMAGE:-farsight-nav:jazzy}"
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker build -t "$IMAGE" "$ROOT/ros2/docker"
OUT="${FARSIGHT_NAV_OUT:-$ROOT/ros2/tests/out}"
mkdir -p "$OUT"
exec docker run --rm --init -v "$ROOT":/farsight:ro -v "$OUT":/out -e SUITE="$SUITE" \
  -e PYTHONDONTWRITEBYTECODE=1 "$IMAGE" bash /farsight/ros2/docker/in-container.sh "$@"
