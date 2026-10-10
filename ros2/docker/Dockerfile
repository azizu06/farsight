# Farsight navigation dev/test image: ROS 2 Jazzy (Ubuntu 24.04 Noble) + Nav2, plus the jetson.server
# Python dependencies so the closed-loop simulator, the bridge and Nav2 run together in one container.
# The apt list matches jetson/ros2-install.sh. Build: docker build -t farsight-nav:jazzy ros2/docker
FROM ros:jazzy-ros-base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ros-jazzy-navigation2 ros-jazzy-nav2-bringup ros-jazzy-pointcloud-to-laserscan \
      ros-jazzy-tf2-ros ros-jazzy-tf-transformations ros-jazzy-rmw-cyclonedds-cpp \
      python3-colcon-common-extensions python3-rosdep python3-websockets python3-numpy \
      python3-pip python3-venv python3-pytest curl \
    && rm -rf /var/lib/apt/lists/*

# jetson.server runs from its own venv (like jetson/.venv on the rover); ROS uses the system Python.
RUN python3 -m venv /opt/farsight-venv && /opt/farsight-venv/bin/pip install --no-cache-dir \
      "fastapi>=0.115,<1" "uvicorn[standard]>=0.30,<1" "opencv-python-headless>=4.10,<5" "numpy>=1.26,<3" \
      "httpx>=0.27,<1" "python-multipart>=0.0.18,<1" "brotli>=1.1,<2" "pyserial>=3.5,<4" pytest
