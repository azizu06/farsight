"""Opt-in TCP forwarder: the Jetson's Tailscale address -> the local Farsight server.

python -m jetson.tailnet_forward [--listen-port 8081] [--upstream 127.0.0.1:8443]

Lets the operator phone reach the Jetson directly over its own Tailscale path
instead of through the rover phone (docs/phone-relay.md, "Direct operator path").
It binds only the Jetson's Tailscale IPv4 address, never 0.0.0.0, and forwards
bytes without parsing them, so HTTP, WebSockets and the 250 ms stop lease behave
exactly as on :8443. Idle connections are never cut; a dead peer is dropped by
TCP keepalive. If the upstream is down the client is closed at once, so the
caller sees a failure instead of a hang. No timeout is lengthened anywhere.
"""

import argparse
import socket
import subprocess
import sys
import threading
import time


def tailscale_ipv4(timeout=3):
    """The Jetson's Tailscale IPv4 address, or None while tailscaled has none yet."""
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True,
                             timeout=timeout, check=False).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out[0] if out and out[0].startswith("100.") else None


def tune(sock):
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for name, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3)):
        if hasattr(socket, name):
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)


def pump(source, sink):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            sink.sendall(data)
    except OSError:
        pass
    finally:
        for sock in (source, sink):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()


def serve(listen, upstream, stop=None):
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(listen)
    server.listen(64)
    server.settimeout(0.5)
    print(f"forwarding {listen[0]}:{listen[1]} -> {upstream[0]}:{upstream[1]}", flush=True)
    try:
        while not (stop and stop.is_set()):
            try:
                client, _ = server.accept()
            except socket.timeout:
                continue
            try:
                remote = socket.create_connection(upstream, timeout=2)
            except OSError:
                client.close()
                continue
            remote.settimeout(None)
            tune(client)
            tune(remote)
            threading.Thread(target=pump, args=(client, remote), daemon=True).start()
            threading.Thread(target=pump, args=(remote, client), daemon=True).start()
    finally:
        server.close()


def parse_upstream(text):
    host, _, port = text.rpartition(":")
    return host or "127.0.0.1", int(port)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--listen-port", type=int, default=8081)
    parser.add_argument("--upstream", default="127.0.0.1:8443")
    parser.add_argument("--listen-host", help="Override the bind address (tests only); default is the Tailscale IPv4")
    args = parser.parse_args(argv)
    upstream = parse_upstream(args.upstream)
    host = args.listen_host
    while host is None:  # tailscaled may still be starting at boot; wait, never bind 0.0.0.0
        host = tailscale_ipv4()
        if host is None:
            print("waiting for a Tailscale IPv4 address", file=sys.stderr, flush=True)
            time.sleep(2)
    try:
        serve((host, args.listen_port), upstream)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
