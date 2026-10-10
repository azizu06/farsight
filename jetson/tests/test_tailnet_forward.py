import socket
import threading
import unittest

from jetson import tailnet_forward


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class EchoServer:
    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.echo, args=(conn,), daemon=True).start()

    def echo(self, conn):
        with conn:
            while data := conn.recv(4096):
                conn.sendall(data)


class ForwarderTests(unittest.TestCase):
    def setUp(self):
        self.echo = EchoServer()
        self.port = free_port()
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=tailnet_forward.serve,
            args=(("127.0.0.1", self.port), ("127.0.0.1", self.echo.port), self.stop), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.stop.set()
        self.thread.join(2)
        self.echo.sock.close()

    def connect(self):
        for _ in range(50):
            try:
                return socket.create_connection(("127.0.0.1", self.port), timeout=2)
            except OSError:
                threading.Event().wait(0.05)
        self.fail("forwarder did not listen")

    def test_bytes_round_trip_in_order(self):
        with self.connect() as sock:
            for chunk in (b"GET /health", b"x" * 100000, b"end"):
                sock.sendall(chunk)
                got = b""
                while len(got) < len(chunk):
                    got += sock.recv(65536)
                self.assertEqual(got, chunk)

    def test_idle_connection_survives(self):
        with self.connect() as sock:
            threading.Event().wait(1.2)
            sock.sendall(b"ping")
            self.assertEqual(sock.recv(16), b"ping")

    def test_upstream_down_closes_client_fast(self):
        self.echo.sock.close()
        dead_port = free_port()
        stop = threading.Event()
        listen = free_port()
        thread = threading.Thread(target=tailnet_forward.serve,
                                  args=(("127.0.0.1", listen), ("127.0.0.1", dead_port), stop), daemon=True)
        thread.start()
        try:
            for _ in range(50):
                try:
                    sock = socket.create_connection(("127.0.0.1", listen), timeout=2)
                    break
                except OSError:
                    threading.Event().wait(0.05)
            sock.settimeout(3)
            self.assertEqual(sock.recv(16), b"")
            sock.close()
        finally:
            stop.set()
            thread.join(2)

    def test_parse_upstream(self):
        self.assertEqual(tailnet_forward.parse_upstream("127.0.0.1:8443"), ("127.0.0.1", 8443))
        self.assertEqual(tailnet_forward.parse_upstream(":9"), ("127.0.0.1", 9))


if __name__ == "__main__":
    unittest.main()
