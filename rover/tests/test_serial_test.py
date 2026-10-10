"""Unit tests for rover/serial_test.py. Run: python -m pytest rover/tests"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serial_test as st  # noqa: E402


def ask(uno, line):
    return st.Link(uno).ask(line)


def test_valid_commands():
    uno = st.FakeUno(range_cm=42)
    assert ask(uno, "M 0 0") == "OK"
    assert ask(uno, "P 90") == "OK"
    assert ask(uno, "R") == "R 42"


def test_invalid_lines_stop_motors():
    uno = st.FakeUno()
    assert ask(uno, "M 50 50") == "OK"
    assert uno.motors == (50, 50)
    assert ask(uno, "M 256 0") == "ERR command"
    assert uno.motors == (0, 0)


def test_overlong_line_errs_once_and_recovers():
    uno = st.FakeUno()
    link = st.Link(uno)
    assert link.ask("M " + "1" * 80) == "ERR line"
    assert link.readline() == ""
    assert link.ask("R") == "R 100"


def test_forward_blocked_by_close_obstacle():
    uno = st.FakeUno(range_cm=10)
    assert ask(uno, "M 50 50") == "OK"
    assert uno.motors == (0, 0)


def test_watchdog_stops_after_300ms():
    uno = st.FakeUno()
    ask(uno, "M 50 50")
    t0 = time.monotonic()
    assert uno.motors == (50, 50)
    ms = (st.sim_observer(uno)() - t0) * 1000
    assert 280 <= ms <= 400


def test_dry_run_never_moves_and_passes():
    class Spy(st.FakeUno):
        def _command(self, text):
            assert not (text.startswith("M") and text.strip() != "M 0 0" and "50" in text)
            super()._command(text)

    lines = []
    assert st.run(Spy(), False, lambda: None, 100, out=lines.append) == 0
    assert any(l.startswith("SKIP watchdog") for l in lines)


def test_motion_run_passes_with_watchdog():
    uno = st.FakeUno()
    assert st.run(uno, True, st.sim_observer(uno), 100, out=lambda *_: None) == 0


def test_missing_watchdog_fails():
    class NoWatchdog(st.FakeUno):
        def _tick(self):
            pass

    uno = NoWatchdog()
    assert st.run(uno, True, st.sim_observer(uno), 100, out=lambda *_: None) == 1


def test_silent_port_fails():
    class Silent:
        def write(self, d): return len(d)
        def read(self, n=1): time.sleep(0.001); return b""
        def reset_input_buffer(self): pass

    assert st.run(Silent(), False, lambda: None, 100, out=lambda *_: None,
                  reply_timeout_s=0.01) == 1


def test_selftest_exits_zero(capsys):
    assert st.main(["--selftest"]) == 0
