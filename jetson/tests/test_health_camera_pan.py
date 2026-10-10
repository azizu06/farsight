import unittest

from fastapi.testclient import TestClient

from jetson.mapping.server import create_app


class HealthCameraPanTests(unittest.TestCase):
    """The viewer picks its default camera mount from /health.camera_pan (--scan-pan)."""

    def health(self, **options):
        with TestClient(create_app(demo=True, simulate_rover=True, detector="none", **options)) as client:
            return client.get("/health").json()

    def test_fixed_phone_camera_reports_no_pan(self):
        self.assertIs(self.health()["camera_pan"], False)

    def test_scan_pan_flag_reports_physical_pan(self):
        self.assertIs(self.health(scan_pan=True)["camera_pan"], True)


if __name__ == "__main__":
    unittest.main()
