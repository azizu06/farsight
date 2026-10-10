"""Actual Mac Network.framework loopback; not iPhone VPN/cellular qualification."""
import base64
import hashlib
import json
import os
from pathlib import Path
import platform
import select
import shutil
import socket
import struct
import subprocess
import tempfile
import concurrent.futures
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(platform.system() == 'Darwin' and shutil.which('swiftc'), 'Apple Network/Swift required')
class PhoneGeometryServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.TemporaryDirectory(prefix='farsight-geometry-server-')
        cls.binary = Path(cls.work.name) / 'fixture'
        subprocess.run(['swiftc', str(ROOT/'ios/FarsightCapture/GeometryWire.swift'),
                        str(ROOT/'ios/FarsightCapture/PhoneGeometryServer.swift'),
                        str(ROOT/'ios/tests/PhoneGeometryServerFixture.swift'), '-o', str(cls.binary)],
                       check=True, capture_output=True, text=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def launch(self, rate=0, bound=8*1024*1024, timeout=8):
        process = subprocess.Popen([str(self.binary), str(rate), str(bound), str(timeout)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.finish, process)
        return process, self.ready(process)

    def finish(self, process):
        if process.poll() is None:
            try:
                process.communicate(b'quit\n', timeout=3)
            except subprocess.TimeoutExpired:
                process.kill(); process.communicate(timeout=3)
        for pipe in (process.stdin, process.stdout, process.stderr):
            pipe.close()
        self.assertEqual(process.returncode, 0)

    def ready(self, process):
        deadline = time.monotonic()+5
        while time.monotonic() < deadline:
            if select.select([process.stdout], [], [], max(0, deadline-time.monotonic()))[0]:
                line = process.stdout.readline().decode()
                if line.startswith('STATE ready '):
                    return int(line.split()[-1])
                if line.startswith('STATE failed'):
                    self.fail(line)
        self.fail('Listener did not become ready')

    def command(self, process, text):
        process.stdin.write((text+'\n').encode()); process.stdin.flush()

    def connect(self, port):
        connection = socket.create_connection(('127.0.0.1', port), timeout=5)
        connection.settimeout(5); self.addCleanup(connection.close)
        return connection

    def read_exact(self, connection, count):
        result = b''
        while len(result)<count:
            chunk = connection.recv(count-len(result))
            if not chunk:
                raise EOFError('connection closed')
            result += chunk
        return result

    def headers(self, connection):
        result = b''
        while not result.endswith(b'\r\n\r\n'):
            result += self.read_exact(connection, 1)
            self.assertLess(len(result), 4096)
        return result

    def websocket(self, port):
        connection = self.connect(port)
        key = base64.b64encode(b'0123456789abcdef').decode()
        connection.sendall(('GET /geometry/stream HTTP/1.1\r\nHost: localhost\r\n'
                            'Upgrade: websocket\r\nConnection: keep-alive, Upgrade\r\n'
                            f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        headers = self.headers(connection)
        self.assertTrue(headers.startswith(b'HTTP/1.1 101 '))
        expected = base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
        self.assertIn(expected, headers)
        return connection

    def frame(self, connection):
        a, b = self.read_exact(connection, 2)
        self.assertEqual(a & 0xf0, 0x80); self.assertEqual(b & 0x80, 0)
        length = b & 127
        if length == 126: length = struct.unpack('>H', self.read_exact(connection, 2))[0]
        if length == 127: length = struct.unpack('>Q', self.read_exact(connection, 8))[0]
        self.assertLessEqual(length, 256*1024)
        return a & 15, self.read_exact(connection, length)

    def initial(self, connection):
        frames = [self.frame(connection) for _ in range(3)]
        self.assertEqual([x[0] for x in frames], [2,2,2])
        self.assertEqual([x[1][1] for x in frames], [2,0,3])
        self.assertEqual([struct.unpack_from('<Q',x[1],4)[0] for x in frames], [1,2,3])
        self.assertEqual(len(frames[0][1]),124)
        self.assertEqual(frames[0][1][:4], b'\x01\x02\x00\x00')

    def control(self, opcode, payload=b''):
        mask=b'1234'
        return bytes([0x80|opcode,0x80|len(payload)])+mask+bytes(x^mask[i%4] for i,x in enumerate(payload))

    def test_http_status_snapshot_binary_ping_close(self):
        process, port = self.launch()
        http = self.connect(port)
        http.sendall(b'GET /geometry/status HTTP/1.1\r\nHost: localhost\r\n\r\n')
        headers=self.headers(http)
        self.assertTrue(headers.startswith(b'HTTP/1.1 200 '))
        length=int(next(x.split(b':',1)[1] for x in headers.split(b'\r\n') if x.startswith(b'Content-Length:')))
        value=json.loads(self.read_exact(http,length))
        self.assertEqual(value['source'],'IPHONE ARKIT MESH'); self.assertTrue(value['ready'])
        connection=self.websocket(port); self.initial(connection)
        # Fragmented masked input must be reassembled without interpreting mesh data.
        ping=self.control(9,b'ping')
        connection.sendall(ping[:3]); connection.sendall(ping[3:])
        self.assertEqual(self.frame(connection),(10,b'ping'))
        connection.sendall(self.control(8,struct.pack('>H',1000)))
        self.assertEqual(self.frame(connection),(8,struct.pack('>H',1000)))
        self.assertEqual(connection.recv(1),b'')

    def test_stop_tears_down_subscriber_and_restart_requires_fresh_snapshot(self):
        process,port=self.launch()
        connection=self.websocket(port); self.initial(connection)
        self.command(process,'stop'); self.assertEqual(connection.recv(1),b'')
        self.command(process,'large')  # stopped publication must never survive restart
        self.command(process,'restart'); fresh=self.websocket(self.ready(process)); self.initial(fresh)
        fresh.settimeout(.15)
        with self.assertRaises(socket.timeout): fresh.recv(1)

    def test_paced_binary_frame_and_latest_anchor_remove_preserve_sequence(self):
        process,port=self.launch(rate=20000)
        connection=self.websocket(port); self.initial(connection)
        started=time.monotonic(); self.command(process,'updates')
        first=self.frame(connection)
        self.assertEqual(struct.unpack_from('<Q',first[1],4)[0],4)
        # 36KB binary record cannot burst immediately through the paced writer.
        self.assertGreater(time.monotonic()-started,1.5)
        last=self.frame(connection)
        self.assertEqual((last[0],last[1][1],struct.unpack_from('<Q',last[1],4)[0]),(2,1,35))
        connection.settimeout(.15)
        with self.assertRaises(socket.timeout): connection.recv(1)

    def test_bounded_barriers_overflow_closes_for_new_snapshot(self):
        process,port=self.launch(rate=10000,bound=1024)
        connection=self.websocket(port); self.initial(connection)
        self.command(process,'overflow')
        connection.settimeout(3)
        # Could already have part of the first frame on the wire; all queued
        # snapshot barriers must cause closure, never arbitrary record dropping.
        while connection.recv(4096): pass
        fresh=self.websocket(port); self.initial(fresh)

    def test_snapshot_barriers_preserve_records_and_third_client_is_rejected(self):
        process,port=self.launch(rate=20000)
        a=self.websocket(port); self.initial(a)
        b=self.websocket(port); self.initial(b)
        third=self.connect(port)
        self.assertEqual(third.recv(1),b'')
        self.command(process,'snapshot')
        for connection in (a,b):
            records=[self.frame(connection)[1] for _ in range(4)]
            self.assertEqual([x[1] for x in records],[2,0,0,3])
            self.assertEqual([struct.unpack_from('<Q',x,4)[0] for x in records],[100,101,102,103])

    def test_shared_aggregate_budget_two_clients_and_write_deadline(self):
        process,port=self.launch(rate=20000)
        a=self.websocket(port); self.initial(a)
        b=self.websocket(port); self.initial(b)
        started=time.monotonic(); self.command(process,'large')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            packets=list(pool.map(self.frame,[a,b]))
        self.assertEqual([len(x[1]) for x in packets],[36136,36136])
        self.assertGreater(time.monotonic()-started,3.4)
        self.assertEqual(packets[0],packets[1])  # whole identical binary messages
        process2,port2=self.launch(rate=20000,timeout=.15)
        slow=self.websocket(port2); self.initial(slow)
        started=time.monotonic(); self.command(process2,'large')
        while slow.recv(4096): pass
        self.assertLess(time.monotonic()-started,1)
        # Deadline drops only that connection and permits a fresh snapshot.
        fresh=self.websocket(port2); self.initial(fresh)

    def test_unmasked_or_oversized_client_frames_and_requests_rejected(self):
        process,port=self.launch()
        connection=self.websocket(port); self.initial(connection)
        connection.sendall(b'\x89\x00')
        self.assertEqual(connection.recv(1),b'')
        connection=self.websocket(port); self.initial(connection)
        connection.sendall(b'\x89\xfe')
        self.assertEqual(connection.recv(1),b'')
        http=self.connect(port); http.sendall(b'GET /geometry/status HTTP/1.1\r\nX: '+b'a'*4100)
        self.assertEqual(http.recv(1),b'')
