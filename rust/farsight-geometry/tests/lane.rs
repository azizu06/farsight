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
    upd_at(seq, [seq as u8; 16], op, m)
}

fn upd_at(seq: u64, anchor: [u8; 16], op: geom::Op, m: Option<geom::ChunkMesh>) -> geom::Update {
    geom::Update {
        session: [3; 16],
        epoch: 7,
        seq,
        op,
        anchor,
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

fn split_records(body: &[u8]) -> Vec<(u64, u8, usize)> {
    // Walk concatenated raw v1 records: (seq, op, header_offset).
    let mut out = Vec::new();
    let mut i = 0;
    while i + 124 <= body.len() {
        assert_eq!(body[i], 1, "record version");
        let op = body[i + 1];
        let seq = u64::from_le_bytes(body[i + 4..i + 12].try_into().unwrap());
        let vc = u32::from_le_bytes(body[i + 116..i + 120].try_into().unwrap()) as usize;
        let ic = u32::from_le_bytes(body[i + 120..i + 124].try_into().unwrap()) as usize;
        out.push((seq, op, i));
        i += 124 + vc * 12 + ic * 4;
    }
    assert_eq!(i, body.len(), "trailing bytes after last record");
    out
}

#[tokio::test]
async fn dense_delta_paging() {
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
            upd(5, geom::Op::Replace, Some(mesh(3))),
            upd_at(6, [3; 16], geom::Op::Remove, None),
        ] {
            ws.send(tokio_tungstenite::tungstenite::Message::Binary(
                geom::encode(&u, geom::RecordCoding::Raw).into(),
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
        if status["last_seq"].as_u64() == Some(6) {
            break;
        }
    }
    // Retained counts for the density audit: anchors [2],[5] survive after
    // the seq-6 Remove of anchor [3].
    assert_eq!(status["retained"]["anchors"], 2);
    assert_eq!(status["retained"]["triangles"], 5);
    assert_eq!(status["retained"]["vertices"], 15);
    assert_eq!(status["retained"]["points"], 15);
    assert_eq!(status["retained"]["bytes"], 15 * 16 + 5 * 12);

    // Bootstrap: Reset + three Replaces + journal Reset, all seq-ordered.
    let resp = client
        .get(format!("http://{addr}/geometry/chunks?after=0"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.headers()["x-geometry-truncated"], "0");
    let body = resp.bytes().await.unwrap();
    let ops: Vec<u8> = split_records(&body).iter().map(|r| r.1).collect();
    assert_eq!(ops, vec![2u8, 2, 0, 0, 1]); // Reset, Reset, Replace x2, Remove
    // Incremental tail past seq 3: Replace(5) + Remove(6).
    let resp = client
        .get(format!("http://{addr}/geometry/chunks?after=3&limit=64"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.headers()["x-geometry-next-seq"], "6");
    let body = resp.bytes().await.unwrap();
    let recs = split_records(&body);
    let seqs: Vec<u64> = recs.iter().map(|r| r.0).collect();
    assert_eq!(seqs, vec![5, 6]);
    let ops: Vec<u8> = recs.iter().map(|r| r.1).collect();
    assert_eq!(ops, vec![0u8, 1u8]); // Replace then Remove
    // Replace(5) carries a mesh; Remove(6) carries none.
    let first = &body[recs[0].2..recs[0].2 + 124];
    let vc = u32::from_le_bytes(first[116..120].try_into().unwrap());
    assert_eq!(vc, 9);
    let last = &body[recs[1].2..recs[1].2 + 124];
    assert_eq!(u32::from_le_bytes(last[116..120].try_into().unwrap()), 0);
    // Delta Replace transforms are identity (world-baked at ingest).
    assert_eq!(f32::from_le_bytes(first[52..56].try_into().unwrap()), 1.0);
    assert_eq!(f32::from_le_bytes(first[112..116].try_into().unwrap()), 1.0);
    // Caught-up consumer: empty body, cursor holds.
    let resp = client
        .get(format!("http://{addr}/geometry/chunks?after=6"))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.headers()["x-geometry-next-seq"], "6");
    assert_eq!(resp.bytes().await.unwrap().len(), 0);
}
