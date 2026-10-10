# farsight_nvblox test image: ROS 2 Jazzy + Nav2 params/launch. nvblox itself needs an NVIDIA GPU and is not installed.
FROM ros:jazzy-ros-base
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ros-jazzy-nav2-bringup ros-jazzy-tf2-ros ros-jazzy-rclcpp-components \
      python3-pytest python3-yaml python3-numpy \
    && rm -rf /var/lib/apt/lists/*
