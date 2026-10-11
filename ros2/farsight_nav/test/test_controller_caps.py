"""Regression checks for the calibrated maximum forward Nav2 speed."""

from pathlib import Path
import re
import unittest


class ControllerCapTests(unittest.TestCase):
    def test_nav2_linear_controllers_request_the_measured_pwm_255_speed(self):
        config_path = Path(__file__).parents[1] / "config" / "nav2_params.yaml"
        text = config_path.read_text()

        def parameter(section, key):
            block = re.search(rf"(?ms)^    {section}:\n(.*?)(?=^    [A-Za-z][A-Za-z0-9_]*:|\Z)", text)
            self.assertIsNotNone(block, f"missing controller section {section}")
            value = re.search(rf"(?m)^      {key}:\s*([0-9.]+)", block.group(1))
            self.assertIsNotNone(value, f"missing {section}.{key}")
            return float(value.group(1))

        # Floor calibration recorded 0.95 m/s at PWM 255. ControlState still
        # clamps the final motor command to the loaded profile and AUTO PWM cap.
        self.assertAlmostEqual(parameter("MPPI", "vx_max"), 0.95)
        self.assertAlmostEqual(parameter("RPP", "desired_linear_vel"), 0.95)
