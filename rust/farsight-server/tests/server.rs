//! Server accept-path tests (socket options, per control/service.py parity).

use axum::serve::Listener;
use farsight_server::server::NodelayListener;

/// Accepted plain sockets get TCP_NODELAY — the same option Python sets so
/// permit/status frames are never coalesced by Nagle on the cellular hop.
#[tokio::test]
async fn accepted_socket_has_tcp_nodelay() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let mut nodelay = NodelayListener(listener);
    let client = tokio::spawn(async move {
        tokio::net::TcpStream::connect(addr).await.unwrap()
    });
    let (io, _) = nodelay.accept().await;
    assert!(io.nodelay().unwrap());
    client.await.unwrap();
}
