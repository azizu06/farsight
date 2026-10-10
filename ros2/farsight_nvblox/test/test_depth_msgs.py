import numpy as np
import pytest

from farsight_nvblox.depth_msgs import camera_info_fields, depth_image_fields

HEADER = dict(fx=105.0, fy=104.0, cx=80.0, cy=60.0)


def test_camera_info_is_a_pinhole_with_the_packet_intrinsics():
    info = camera_info_fields(HEADER, 160, 120)
    assert (info["width"], info["height"]) == (160, 120)
    assert info["k"] == [105.0, 0.0, 80.0, 0.0, 104.0, 60.0, 0.0, 0.0, 1.0]
    assert info["p"][:3] == [105.0, 0.0, 80.0] and info["p"][5:7] == [104.0, 60.0]
    assert info["d"] == [0.0] * 5 and info["distortion_model"] == "plumb_bob"


@pytest.mark.parametrize("bad", [dict(HEADER, fx=0.0), dict(HEADER, fy=-1.0), dict(HEADER, cx=float("nan"))])
def test_camera_info_rejects_unusable_intrinsics(bad):
    with pytest.raises(ValueError):
        camera_info_fields(bad, 160, 120)


def test_camera_info_requires_every_intrinsic():
    with pytest.raises(KeyError):
        camera_info_fields({"fx": 1.0}, 4, 4)


def test_depth_image_is_float32_metres_with_invalid_samples_zeroed():
    depth = np.array([[1.5, 0.0, np.nan], [np.inf, -2.0, 4.0]], np.float32)
    image = depth_image_fields(depth)
    assert (image["height"], image["width"], image["encoding"], image["step"]) == (2, 3, "32FC1", 12)
    out = np.frombuffer(image["data"], "<f4").reshape(2, 3)
    assert out.tolist() == [[1.5, 0.0, 0.0], [0.0, 0.0, 4.0]]


def test_depth_image_rejects_non_2d():
    with pytest.raises(ValueError):
        depth_image_fields(np.zeros((2, 3, 4)))
