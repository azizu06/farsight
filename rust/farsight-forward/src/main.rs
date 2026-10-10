//! Opt-in TCP forwarder: the Jetson's Tailscale address -> the local Farsight server.
//!
//! Rust port of `jetson/tailnet_forward.py`. Lets the operator phone reach the
//! Jetson directly over its own Tailscale path instead of through the rover
//! phone (docs/phone-relay.md, "Direct operator path"). It binds only the
//! Jetson's Tailscale IPv4 address, never 0.0.0.0, and forwards bytes without
//! parsing them, so HTTP, WebSockets and the 250 ms stop lease behave exactly
//! as on :8443. Idle connections are never cut; a dead peer is dropped by TCP
//! keepalive. If the upstream is down the client is closed at once, so the
//! caller sees a failure instead of a hang. No timeout is lengthened anywhere.

use std::io::{self, Read, Write};
use std::net::{Shutdown, TcpListener, TcpStream, ToSocketAddrs};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use clap::Parser;
use socket2::{Domain, SockRef, Socket, TcpKeepalive, Type};

/// `source.recv(65536)` — one blocking read per write.
const CHUNK: usize = 65536;
/// `server.listen(64)`.
const BACKLOG: i32 = 64;
/// `socket.create_connection(upstream, timeout=2)`.
const CONNECT_TIMEOUT: Duration = Duration::from_secs(2);
/// `subprocess.run(["tailscale", "ip", "-4"], timeout=3)`.
const TAILSCALE_TIMEOUT: Duration = Duration::from_secs(3);
/// `time.sleep(2)` between Tailscale address polls.
const TAILSCALE_RETRY: Duration = Duration::from_secs(2);

/// The Jetson's Tailscale IPv4 address, or None while tailscaled has none yet.
/// Mirrors `tailscale_ipv4(timeout=3)`: first token of `tailscale ip -4` that
/// starts with "100."; OSError or a timeout returns None.
fn tailscale_ipv4() -> Option<String> {
    let mut child = Command::new("tailscale")
        .args(["ip", "-4"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let deadline = Instant::now() + TAILSCALE_TIMEOUT;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(25)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
            Err(_) => return None,
        }
    }
    let mut out = String::new();
    child.stdout.take()?.read_to_string(&mut out).ok()?;
    let first = out.split_whitespace().next()?;
    first.starts_with("100.").then(|| first.to_string())
}

/// `tune(sock)`: TCP_NODELAY + SO_KEEPALIVE with KEEPIDLE=10, INTVL=5, KEEPCNT=3.
/// Python guards each option with hasattr; the equivalent here is ignoring
/// per-option errors so an unsupported platform still gets the rest.
fn tune(sock: &TcpStream) {
    let sock = SockRef::from(sock);
    let _ = sock.set_tcp_nodelay(true);
    let keepalive = TcpKeepalive::new()
        .with_time(Duration::from_secs(10))
        .with_interval(Duration::from_secs(5))
        .with_retries(3);
    let _ = sock.set_tcp_keepalive(&keepalive);
}

/// `pump(source, sink)`: blocking recv/sendall until EOF or error, then
/// shutdown(SHUT_RDWR) + close on BOTH sockets. Whichever direction ends first
/// tears the whole connection down, so the other pump's blocked recv wakes with
/// an error and its own (idempotent) shutdown runs. try_clone'd descriptors
/// refer to the same kernel sockets, so shutting down here hits both pumps.
fn pump(mut source: TcpStream, mut sink: TcpStream) {
    let mut buf = [0u8; CHUNK];
    loop {
        match source.read(&mut buf) {
            Err(_) | Ok(0) => break,
            Ok(n) => {
                if sink.write_all(&buf[..n]).is_err() {
                    break;
                }
            }
        }
    }
    let _ = source.shutdown(Shutdown::Both);
    let _ = sink.shutdown(Shutdown::Both);
}

/// `socket.create_connection(upstream, timeout=2)`, then blocking mode. Tries
/// each resolved address like CPython's create_connection.
fn connect_upstream(host: &str, port: u16) -> io::Result<TcpStream> {
    let mut last = io::Error::new(io::ErrorKind::AddrNotAvailable, "no address resolved");
    for addr in (host, port).to_socket_addrs()? {
        match TcpStream::connect_timeout(&addr, CONNECT_TIMEOUT) {
            Ok(stream) => return Ok(stream),
            Err(e) => last = e,
        }
    }
    Err(last)
}

/// `serve(listen, upstream)`: accept loop; on upstream failure the client is
/// closed at once so the caller sees a failure instead of a hang.
fn serve(listener: TcpListener, upstream: (String, u16)) -> io::Result<()> {
    let listen = listener.local_addr()?;
    println!("forwarding {}:{} -> {}:{}", listen.ip(), listen.port(), upstream.0, upstream.1);
    for client in listener.incoming() {
        let client = match client {
            Ok(client) => client,
            Err(_) => continue,
        };
        let remote = match connect_upstream(&upstream.0, upstream.1) {
            Ok(remote) => remote,
            Err(_) => {
                drop(client); // client.close()
                continue;
            }
        };
        tune(&client);
        tune(&remote);
        // Two daemon pump threads like Python; try_clone keeps one descriptor
        // per direction while shutdown() still lands on the shared socket.
        let client_to_remote = client.try_clone();
        let remote_to_client = remote.try_clone();
        match (client_to_remote, remote_to_client) {
            (Ok(c2), Ok(r2)) => {
                thread::spawn(move || pump(c2, r2));
                thread::spawn(move || pump(remote, client));
            }
            _ => {
                drop(client);
                drop(remote);
            }
        }
    }
    Ok(())
}

/// `server = socket.socket(); SO_REUSEADDR; bind; listen(64)`. Resolves the
/// bind host like CPython's socket.bind (names go through getaddrinfo too).
fn bind(host: &str, port: u16) -> io::Result<TcpListener> {
    let addr = (host, port)
        .to_socket_addrs()?
        .next()
        .ok_or_else(|| io::Error::new(io::ErrorKind::AddrNotAvailable, format!("no address for {host}")))?;
    let socket = Socket::new(
        if addr.is_ipv4() { Domain::IPV4 } else { Domain::IPV6 },
        Type::STREAM,
        None,
    )?;
    socket.set_reuse_address(true)?;
    socket.bind(&addr.into())?;
    socket.listen(BACKLOG)?;
    Ok(socket.into())
}

/// `parse_upstream(text)`: rpartition(":") with an empty host meaning 127.0.0.1.
fn parse_upstream(text: &str) -> io::Result<(String, u16)> {
    let invalid = || io::Error::new(io::ErrorKind::InvalidInput, format!("invalid upstream: {text}"));
    let (host, port) = match text.rsplit_once(':') {
        Some((host, port)) => (if host.is_empty() { "127.0.0.1" } else { host }, port),
        None => ("127.0.0.1", text),
    };
    let port: u16 = port.parse().map_err(|_| invalid())?;
    Ok((host.to_string(), port))
}

/// Opt-in TCP forwarder: the Jetson's Tailscale address -> the local Farsight server.
#[derive(Parser)]
#[command(about = "Opt-in TCP forwarder: the Jetson's Tailscale address -> the local Farsight server",
          long_about = None)]
struct Args {
    #[arg(long, default_value_t = 8081)]
    listen_port: u16,
    #[arg(long, default_value = "127.0.0.1:8443")]
    upstream: String,
    /// Override the bind address (tests only); default is the Tailscale IPv4
    #[arg(long)]
    listen_host: Option<String>,
}

fn main() -> io::Result<()> {
    // argparse parity: --help exits 0, bad args exit 2 with usage on stderr.
    let args = Args::parse();
    let upstream = parse_upstream(&args.upstream)?;
    let host = match args.listen_host {
        Some(host) => host,
        // tailscaled may still be starting at boot; wait, never bind 0.0.0.0.
        None => loop {
            match tailscale_ipv4() {
                Some(host) => break host,
                None => {
                    eprintln!("waiting for a Tailscale IPv4 address");
                    thread::sleep(TAILSCALE_RETRY);
                }
            }
        },
    };
    serve(bind(&host, args.listen_port)?, upstream)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::Ipv4Addr;
    use std::sync::mpsc;
    use std::time::Duration;

    fn loopback() -> (TcpListener, u16) {
        let listener = bind("127.0.0.1", 0).unwrap();
        let port = listener.local_addr().unwrap().port();
        (listener, port)
    }

    /// Spawn serve() on a detached thread; the test process owns cleanup.
    fn spawn_forward(upstream_port: u16) -> u16 {
        let (listener, port) = loopback();
        thread::spawn(move || {
            let _ = serve(listener, ("127.0.0.1".to_string(), upstream_port));
        });
        // Give the accept loop a moment to be ready; connects queue anyway.
        port
    }

    fn connect(port: u16) -> TcpStream {
        TcpStream::connect(("127.0.0.1", port)).unwrap()
    }

    #[test]
    fn pump_flows_both_ways() {
        // Fake upstream: greet, then echo until EOF.
        let (up_listener, up_port) = loopback();
        let (done_tx, done_rx) = mpsc::channel();
        thread::spawn(move || {
            let (mut s, _) = up_listener.accept().unwrap();
            s.write_all(b"HELLO").unwrap();
            let mut buf = [0u8; 4096];
            loop {
                match s.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(n) => s.write_all(&buf[..n]).unwrap(),
                }
            }
            done_tx.send(()).unwrap();
        });
        let port = spawn_forward(up_port);
        let mut client = connect(port);
        client.set_read_timeout(Some(Duration::from_secs(5))).unwrap();

        // Upstream -> client direction.
        let mut greeting = [0u8; 5];
        client.read_exact(&mut greeting).unwrap();
        assert_eq!(&greeting, b"HELLO");

        // Client -> upstream -> client direction, including >64 KiB to prove
        // chunked recv/sendall keeps order.
        let payload: Vec<u8> = (0..200_000u32).map(|i| (i % 251) as u8).collect();
        client.write_all(&payload).unwrap();
        let mut echoed = vec![0u8; payload.len()];
        client.read_exact(&mut echoed).unwrap();
        assert_eq!(echoed, payload);

        // Full close propagates: client FIN ends the upstream's read loop.
        client.shutdown(Shutdown::Both).unwrap();
        done_rx.recv_timeout(Duration::from_secs(5)).unwrap();
    }

    #[test]
    fn half_close_tears_down_both_directions() {
        // Python semantics: the first pump to end shuts down BOTH sockets, so a
        // client FIN reaches the upstream as EOF AND kills remote->client.
        let (up_listener, up_port) = loopback();
        let (eof_tx, eof_rx) = mpsc::channel();
        let (write_tx, write_rx) = mpsc::channel::<bool>();
        thread::spawn(move || {
            let (mut s, _) = up_listener.accept().unwrap();
            let mut buf = [0u8; 4096];
            // Read until the forwarder tears the socket down.
            loop {
                match s.read(&mut buf) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => continue,
                }
            }
            eof_tx.send(()).unwrap();
            // The shared socket is shut down: writes must now fail (a few
            // tries; the RST may take a packet to arrive).
            let mut failed = false;
            for _ in 0..20 {
                if s.write_all(b"after-close").is_err() {
                    failed = true;
                    break;
                }
                thread::sleep(Duration::from_millis(25));
            }
            write_tx.send(failed).unwrap();
        });
        let port = spawn_forward(up_port);
        let client = connect(port);
        client.shutdown(Shutdown::Write).unwrap();

        eof_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        assert!(write_rx.recv_timeout(Duration::from_secs(5)).unwrap());
        // The client's own socket was shut down too: reading hits EOF/reset.
        let mut client = client;
        client.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        let mut buf = [0u8; 16];
        match client.read(&mut buf) {
            Ok(0) | Err(_) => {}
            Ok(n) => panic!("expected closed socket, read {n} bytes"),
        }
    }

    #[test]
    fn upstream_down_closes_client_at_once() {
        // Nothing listening on the upstream port: the accepted client must see
        // an immediate close, not a hang. Grab then release a port so it is
        // almost certainly closed.
        let dead_port = TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        let port = spawn_forward(dead_port);
        let mut client = connect(port);
        client.set_read_timeout(Some(Duration::from_secs(5))).unwrap();
        let mut buf = [0u8; 16];
        match client.read(&mut buf) {
            Ok(0) => {}
            Err(_) => {}
            Ok(n) => panic!("expected immediate close, read {n} bytes"),
        }
    }

    #[test]
    fn tune_sets_python_socket_options() {
        // Options live on the tuned fd (invisible to the peer), so assert the
        // getsockopt values on a socket after tune() runs on it.
        let (listener, port) = loopback();
        let peer = connect(port);
        let (stream, _) = listener.accept().unwrap();
        tune(&stream);
        let sock = SockRef::from(&stream);
        assert!(sock.tcp_nodelay().unwrap(), "TCP_NODELAY must be set");
        assert!(sock.keepalive().unwrap(), "SO_KEEPALIVE must be set");
        assert_eq!(sock.tcp_keepalive_time().unwrap(), Duration::from_secs(10));
        assert_eq!(sock.tcp_keepalive_interval().unwrap(), Duration::from_secs(5));
        assert_eq!(sock.tcp_keepalive_retries().unwrap(), 3);
        drop(stream);
        drop(peer);
    }

    #[test]
    fn parse_upstream_matches_python_rpartition() {
        assert_eq!(parse_upstream("127.0.0.1:8443").unwrap(), ("127.0.0.1".to_string(), 8443));
        assert_eq!(parse_upstream(":8443").unwrap(), ("127.0.0.1".to_string(), 8443));
        assert_eq!(parse_upstream("jetson.tailnet.ts.net:8443").unwrap(),
                   ("jetson.tailnet.ts.net".to_string(), 8443));
        assert!(parse_upstream("127.0.0.1").is_err()); // int("127.0.0.1") fails in Python too
        assert!(parse_upstream("127.0.0.1:abc").is_err());
    }
}
