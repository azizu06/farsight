import threading
import time
import unittest

from jetson.control.serial_link import SerialPump
from jetson.control.state import ControlState


class FakeUno:
    """Byte-level serial fake, deliberately not evidence of real Uno wiring."""
    def __init__(self):
        self.lock = threading.Lock()
        self.replies = bytearray()
        self.writes = []
        self.failed = False
        self.closed = False

    @property
    def in_waiting(self):
        with self.lock:
            return len(self.replies)

    def read(self, size):
        if self.failed:
            raise OSError("USB disconnected")
        with self.lock:
            reply = self.replies[:size]
            del self.replies[:size]
            return reply

    def write(self, data):
        if self.failed:
            raise OSError("USB disconnected")
        with self.lock:
            self.writes.append((time.monotonic(), data))
            for line in data.splitlines():
                self.replies.extend(b"R 120\n" if line == b"R" else b"OK\n")
        return len(data)

    def reset_input_buffer(self):
        self.replies.clear()

    def reset_output_buffer(self):
        pass

    def close(self):
        self.closed = True


class SerialTests(unittest.TestCase):
    def wait_for(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("serial condition timed out")
            time.sleep(0.01)

    def test_wire_pump_stop_deadline_and_reconnect_no_replay(self):
        state = ControlState()
        first, second = FakeUno(), FakeUno()
        devices = [first, second]
        calls = []

        def factory(port, baud, **options):
            calls.append((port, baud, options))
            return devices.pop(0)

        pump = SerialPump(state, port="FAKE", factory=factory)
        pump.start()
        try:
            self.wait_for(lambda: state.connected and state.range_cm == 120)
            self.assertEqual(calls[0][1], 115200)
            self.assertTrue(first.writes[0][1].startswith(b"M 0 0\n"))
            session = state.begin()
            state.accept({"session_id": session, "seq": 0, "deadman": False}, session)
            token = state.status(issue_permit=True)["permit"]
            state.accept({"session_id": session, "permit": token, "seq": 1,
                          "deadman": True, "v": 0.5, "w": 0, "video_fresh": True,
                          "video_age_ms": 0}, session)
            self.wait_for(lambda: any(b"M 50 50\n" in data for _, data in first.writes))
            # Simulate the network/application going silent. Serial continues
            # pumping, but the motor command becomes zero without any WS task.
            expiry = state.deadline
            self.wait_for(lambda: state.requires_release)
            self.wait_for(lambda: any(at >= expiry and b"M 0 0\n" in data
                                     for at, data in first.writes))
            first.failed = True
            self.wait_for(lambda: not state.connected)
            self.wait_for(lambda: state.connected and len(calls) == 2, timeout=4)
            self.assertTrue(second.writes[0][1].startswith(b"M 0 0\n"))
            self.assertTrue(state.requires_release)
            self.assertFalse(state.accept({"session_id": session, "permit": token,
                                          "seq": 2, "deadman": True, "v": 0.5, "w": 0,
                                          "video_fresh": True, "video_age_ms": 0}, session))
            self.assertEqual(state.output()[:2], (0, 0))
        finally:
            pump.close()
        self.assertTrue(second.closed)
        self.assertFalse(state.connected)

    def test_backend_selection_is_explicit(self):
        state = ControlState()
        pump = SerialPump(state)
        pump.start()
        self.assertIsNone(pump.thread)
        self.assertFalse(state.connected)
        self.assertEqual(state.reason, "rover_disabled")
        with self.assertRaises(ValueError):
            SerialPump(state, port="FAKE", simulate=True)


if __name__ == "__main__":
    unittest.main()
