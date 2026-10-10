"""Unit tests for rover/wheel_bench.py against the serial_test FakeUno. Run: python -m pytest rover/tests"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serial_test as st  # noqa: E402
import wheel_bench as wb  # noqa: E402


class Recorder(st.FakeUno):
    """FakeUno that records every motor state it applies."""

    def __init__(self):
        super().__init__()
        self.seen = []

    def _command(self, text):
        super()._command(text)
        self.seen.append((self._left, self._right))


def test_spin_caps_pwm_and_ends_stopped():
    uno = Recorder()
    assert wb.spin(st.Link(uno), "left", "rev", 200, 0.25, cap=120, out=lambda *_: None)
    assert (-120, 0) in uno.seen
    assert all(abs(left) <= 120 and abs(right) <= 120 for left, right in uno.seen)
    assert uno.motors == (0, 0)


def test_single_side_forward_runs_without_range_sensor():
    uno = Recorder()
    assert wb.spin(st.Link(uno), "right", "fwd", 80, 0.2, cap=120, out=lambda *_: None)
    assert (0, 80) in uno.seen
    assert uno.motors == (0, 0)


def test_cap_above_limit_rejected():
    try:
        wb.main(["--port", "/dev/null", "--cap", "200", "sides"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("cap above 120 accepted")
