# SLAM test image: ROS 2 Jazzy (Ubuntu 24.04 Noble) + SLAM Toolbox + pointcloud_to_laserscan.
# Same apt packages listed for the Jetson in docs/navigation.md, so results transfer.
FROM ros:jazzy-ros-base

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ros-jazzy-slam-toolbox ros-jazzy-pointcloud-to-laserscan \
      ros-jazzy-tf2-ros ros-jazzy-tf-transformations ros-jazzy-launch-testing \
      python3-numpy python3-psutil python3-pytest \
    && rm -rf /var/lib/apt/lists/*
