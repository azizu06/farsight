//! End-to-end: a WS producer pushes v1 records, the pull loop assembles, and
//! the served mesh.bin carries exact wire bytes + ETag/304 semantics.

use farsight_geometry as geom;
use futures_util::SinkExt;
use serde_json::json;

fn mesh(tris: usize) -> geom::ChunkMesh {
    let mut positions = Vec::new();
    let mut colors = Vec::new();
    let mut indices = Vec::new();
    for _ in 0..tris {
        let base = positions.len() as u32;
        positions.extend_from_slice(&[[0.0; 3], [1.0; 3], [0.0, 1.0, 0.0]]);
        colors.extend_from_slice(&[[7, 8, 9]; 3]);
        indices.push([base, base + 1, base + 2]);
    }
    geom::ChunkMesh { positions, colors, indices }
}

fn upd(seq: u64, op: geom::Op, m: Option<geom::ChunkMesh>) -> geom::Update {
    geom::Update {
        session: [3; 16],
        epoch: 7,
        seq,
        op,
        anchor: [seq as u8; 16],
        transform: {
            let mut m = [0.0f32; 16];
            m[0] = 1.0; m[5] = 1.0; m[10] = 1.0; m[15] = 1.0;
            m
        },
        mesh: m,
    }
}

#[tokio::test]
async fn pull_assemble_serve() {
    // Producer: one WS listener sending reset + two replaces + snapshot-end.
    let producer = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let prod_addr = producer.local_addr().unwrap();
    tokio::spawn(async move {
        let (stream, _) = producer.accept().await.unwrap();
        let mut ws = tokio_tungstenite::accept_async(stream).await.unwrap();
        for u in [
            upd(1, geom::Op::Reset, None),
            upd(2, geom::Op::Replace, Some(mesh(2))),
            upd(3, geom::Op::Replace, Some(mesh(1))),
            upd(4, geom::Op::SnapshotEnd, None),
        ] {
            ws.send(tokio_tungstenite::tungstenite::Message::Binary(
                geom::encode(&u, geom::RecordCoding::Zlib).into(),
            ))
            .await
            .unwrap();
        }
        futures_util::future::pending::<()>().await;
    });

    let shared = geom::Shared::new(10_000);
    tokio::spawn(geom::pull_loop(
        shared.clone(),
        vec![format!("ws://{prod_addr}/geometry/stream")],
        0.05,
        0.2,
    ));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, geom::router(shared)).await.unwrap();
    });

    let client = reqwest::Client::new();
    let mut status = json!({});
    for _ in 0..50 {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        status = client
            .get(format!("http://{addr}/geometry/status"))
            .send()
            .await
            .unwrap()
            .json()
            .await
            .unwrap();
        if status["triangles"].as_u64() == Some(3) {
            break;
        }
    }
    assert_eq!(status["triangles"], 3);
    assert_eq!(status["anchors"], 2);
    assert_eq!(status["epoch"], 7);
    assert_eq!(status["lane"]["dropped_malformed"], 0);

    // mesh.bin wire shape + ETag/304
    let resp = client
        .get(format!("http://{addr}/geometry/mesh.bin"))
        .send()
        .await
        .unwrap();
    let etag = resp.headers()["etag"].to_str().unwrap().to_string();
    let body = resp.bytes().await.unwrap();
    let vcount = u32::from_le_bytes(body[0..4].try_into().unwrap());
    let icount = u32::from_le_bytes(body[4..8].try_into().unwrap());
    assert_eq!((vcount, icount), (9, 9));
    assert_eq!(body.len(), 8 + 9 * 16 + 9 * 4);
    let resp = client
        .get(format!("http://{addr}/geometry/mesh.bin"))
        .header("if-none-match", &etag)
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 304);
}
