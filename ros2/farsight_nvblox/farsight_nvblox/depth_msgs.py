"""Depth Image + CameraInfo for nvblox from a farsight depth packet header. Numpy only; ROS is imported lazily.

jetson/nav.py serves one /ws/nav bridge client, so nvblox cannot open its own depth stream: the core bridge
decodes each depth packet once and calls DepthPublisher.publish() (or builds the same messages itself).

Contract: topics `depth/image` (32FC1, metres, 0 = no return) and `depth/camera_info` in `camera_optical`
(x right, y down, z forward), stamped with the same clock the bridge uses for its TF so nvblox's
odom -> camera_optical lookup finds the pose of that frame.
"""

import numpy as np

DEFAULT_FRAME = "camera_optical"


def camera_info_fields(header, width, height):
    """Pinhole CameraInfo values (no distortion) from the depth packet header's fx, fy, cx, cy."""
    fx, fy, cx, cy = (float(header[key]) for key in ("fx", "fy", "cx", "cy"))
    if not all(np.isfinite(v) for v in (fx, fy, cx, cy)) or fx <= 0 or fy <= 0:
        raise ValueError("Depth intrinsics must be finite with positive focal lengths")
    return dict(width=int(width), height=int(height), distortion_model="plumb_bob", d=[0.0] * 5,
                k=[fx, 0.0, cx, 0.0, fy, cy, 0.0, 0.0, 1.0],
                r=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                p=[fx, 0.0, cx, 0.0, 0.0, fy, cy, 0.0, 0.0, 0.0, 1.0, 0.0])


def depth_image_fields(depth):
    """Metres (H x W, 0 = no return) -> Image fields. Non-finite or negative samples become 0."""
    depth = np.asarray(depth, np.float32)
    if depth.ndim != 2:
        raise ValueError("Depth must be a 2D array")
    clean = np.where(np.isfinite(depth) & (depth > 0), depth, 0.0).astype("<f4")
    height, width = clean.shape
    return dict(height=height, width=width, encoding="32FC1", is_bigendian=0, step=width * 4,
                data=clean.tobytes())


class DepthPublisher:
    """Publish one decoded depth frame as Image + CameraInfo on `image_topic` and `info_topic`."""

    def __init__(self, node, image_topic="depth/image", info_topic="depth/camera_info", frame_id=DEFAULT_FRAME,
                 qos_depth=2):
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CameraInfo, Image
        self._node, self._frame, self._Image, self._CameraInfo = node, frame_id, Image, CameraInfo
        self.image = node.create_publisher(Image, image_topic, qos_profile_sensor_data)
        self.info = node.create_publisher(CameraInfo, info_topic, qos_profile_sensor_data)

    def publish(self, header, depth, stamp=None):
        """header: decoded depth packet header; depth: float32 metres. Returns the stamp used."""
        stamp = stamp or self._node.get_clock().now().to_msg()
        fields = depth_image_fields(depth)
        info_fields = camera_info_fields(header, fields["width"], fields["height"])
        image = self._Image(**{k: v for k, v in fields.items() if k != "data"})
        image.data = fields["data"]
        image.header.stamp, image.header.frame_id = stamp, self._frame
        info = self._CameraInfo(**info_fields)
        info.header.stamp, info.header.frame_id = stamp, self._frame
        self.info.publish(info)
        self.image.publish(image)
        return stamp
