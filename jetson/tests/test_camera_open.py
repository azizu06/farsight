import unittest
from unittest import mock

from jetson import camera


class FakeCapture:
    opened = True

    def __init__(self, *args):
        self.args = args
        self.sets = []

    def isOpened(self):
        return self.opened

    def release(self):
        self.released = True

    def set(self, prop, value):
        self.sets.append((prop, value))
        return True


class OpenCaptureTests(unittest.TestCase):
    def open(self, device, platform="linux", opened=True):
        FakeCapture.opened = opened
        with mock.patch.object(camera.cv2, "VideoCapture", FakeCapture):
            return camera.open_capture(device, 640, 480, 15, platform=platform)

    def test_linux_path_uses_v4l2_backend_and_asks_for_mjpg_first(self):
        capture = self.open("/dev/video0")
        self.assertEqual(capture.args, ("/dev/video0", camera.cv2.CAP_V4L2))
        props = [prop for prop, _ in capture.sets]
        self.assertLess(props.index(camera.cv2.CAP_PROP_FOURCC), props.index(camera.cv2.CAP_PROP_FRAME_WIDTH))
        self.assertIn((camera.cv2.CAP_PROP_FOURCC, camera.cv2.VideoWriter_fourcc(*"MJPG")), capture.sets)
        self.assertIn((camera.cv2.CAP_PROP_FRAME_WIDTH, 640), capture.sets)
        self.assertIn((camera.cv2.CAP_PROP_BUFFERSIZE, 1), capture.sets)

    def test_numeric_index_becomes_int(self):
        self.assertEqual(self.open("0").args, (0, camera.cv2.CAP_V4L2))

    def test_other_platforms_use_default_backend(self):
        self.assertEqual(self.open("0", platform="darwin").args, (0,))

    def test_unopenable_device_raises_with_hint(self):
        with self.assertRaises(RuntimeError) as caught:
            self.open("/dev/video9", opened=False)
        self.assertIn("/dev/video9", str(caught.exception))
        self.assertIn("v4l2-ctl", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
