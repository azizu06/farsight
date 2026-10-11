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

#[tokio::test]
async fn delta_pagination_rules() {
    // Aziz's densemap contract: fresh or stale consumers always land exactly
    // one complete snapshot; session/epoch headers let them detect swaps.
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
    for _ in 0..50 {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        let st: serde_json::Value = client
            .get(format!("http://{addr}/geometry/status"))
            .send().await.unwrap().json().await.unwrap();
        if st["anchors"].as_u64() == Some(2) {
            break;
        }
    }

    // Fresh consumer: exactly one synthetic Reset first, then the complete
    // retained set, then the journaled tail — seqs never decrease.
    let resp = client
        .get(format!("http://{addr}/geometry/chunks"))
        .send().await.unwrap();
    assert_eq!(resp.headers()["x-geometry-truncated"], "0");
    assert!(resp.headers().contains_key("x-geometry-session"));
    assert_eq!(resp.headers()["x-geometry-epoch"], "7");
    let recs = split_records(&resp.bytes().await.unwrap());
    let ops: Vec<u8> = recs.iter().map(|r| r.1).collect();
    assert_eq!(ops[0], 2); // Reset first
    assert_eq!(ops.iter().filter(|&&o| o == 2).count(), 2, "synthetic + journaled session Reset only");
    let seqs: Vec<u64> = recs.iter().map(|r| r.0).collect();
    assert!(seqs.windows(2).all(|w| w[0] <= w[1]), "seqs monotonic: {seqs:?}");

    // Stale cursor ahead of every journaled seq (e.g. missed a session
    // swap): still lands one complete snapshot, flagged truncated.
    let resp = client
        .get(format!("http://{addr}/geometry/chunks?after=9999"))
        .send().await.unwrap();
    assert_eq!(resp.headers()["x-geometry-truncated"], "1");
    let recs = split_records(&resp.bytes().await.unwrap());
    let ops: Vec<u8> = recs.iter().map(|r| r.1).collect();
    assert_eq!(ops[0], 2);
    assert!(ops.iter().filter(|&&o| o == 0).count() >= 2, "all retained chunks emitted");

    // Caught-up page has no synthetic Reset — only journaled ops.
    let resp = client
        .get(format!("http://{addr}/geometry/chunks?after=4"))
        .send().await.unwrap();
    assert_eq!(resp.bytes().await.unwrap().len(), 0);
}

/// Spawn the standard producer (reset + 2 replaces + snapshot-end) and serve
/// the geometry router on an ephemeral port; waits for the published
/// snapshot. Returns the bind address.
async fn serve_published() -> (std::net::SocketAddr, std::sync::Arc<geom::Shared>) {
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
    tokio::spawn({
        let shared = shared.clone();
        async move {
            axum::serve(listener, geom::router(shared)).await.unwrap();
        }
    });
    let client = reqwest::Client::new();
    for _ in 0..60 {
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
        let st: serde_json::Value = client
            .get(format!("http://{addr}/geometry/status"))
            .send().await.unwrap().json().await.unwrap();
        if st["snapshot_complete"].as_bool() == Some(true) && st["triangles"].as_u64() == Some(3) {
            return (addr, shared);
        }
    }
    panic!("snapshot never published");
}

const CONFLICT_BODY: &str = "Phone reconstruction cannot be reset or paused here. Stop and restart phone capture to reset its AR world; native geometry commands are not available yet.";

#[tokio::test]
async fn map_routes_mirror_the_puller() {
    let (addr, _shared) = serve_published().await;
    let client = reqwest::Client::new();

    // /map/reset + /map/pause: exact 409 body for every method.
    for path in ["/map/reset", "/map/pause"] {
        for method in [reqwest::Method::GET, reqwest::Method::POST] {
            let resp = client
                .request(method, format!("http://{addr}{path}"))
                .send().await.unwrap();
            assert_eq!(resp.status(), 409);
            assert_eq!(resp.text().await.unwrap(), CONFLICT_BODY);
        }
    }

    // Merged status: puller status() shape + untouched upstream keys.
    let status: serde_json::Value = client
        .get(format!("http://{addr}/map/status"))
        .send().await.unwrap().json().await.unwrap();
    assert_eq!(status["type"], "status");
    assert_eq!(status["geometry_source"], "IPHONE ARKIT MESH");
    assert_eq!(status["points_transport"], "http");
    assert_eq!(status["connected"], true);
    assert_eq!(status["fresh"], true);
    assert_eq!(status["session_id"], status["session"]);
    assert_eq!(status["map_epoch"], 7);
    assert_eq!(status["points"], 9);
    assert_eq!(status["triangles"], 3);
    assert_eq!(status["mesh_revision"], status["revision"]);
    let g = &status["geometry"];
    assert_eq!(g["source"], "IPHONE ARKIT MESH");
    assert_eq!(g["matched"], true);
    assert_eq!(g["retained"], false);
    assert_eq!(g["connected"], true);
    assert_eq!(g["snapshot_complete"], true);
    assert_eq!(g["partial"], false);
    assert_eq!(g["anchors"], 2);
    assert_eq!(g["revision"], status["revision"]);
    assert_eq!(g["receiver_revision"], status["revision"]);
    assert_eq!(g["last_seq"], 4);
    // Upstream contract keys intact for puller/qualify consumers.
    assert!(status["lane"]["upstream"].is_string());
    assert_eq!(status["retained"]["points"], 9);
    assert_eq!(status["observed"]["triangles"], 3);

    let mesh = client
        .get(format!("http://{addr}/map/mesh.bin"))
        .send().await.unwrap();
    let mesh_headers = mesh.headers().clone();
    let mesh_body = mesh.bytes().await.unwrap();
    assert_eq!(mesh_headers["x-geometry-source"], "IPHONE ARKIT MESH");
    assert_eq!(mesh_headers["x-geometry-retained"], "false");
    assert_eq!(mesh_headers["x-map-session"], status["session_id"].as_str().unwrap());
    assert_eq!(mesh_headers["x-map-epoch"], "7");
    assert_eq!(mesh_headers["x-map-generation"], status["generation"].as_u64().unwrap().to_string());
    assert_eq!(mesh_headers["x-map-revision"], status["revision"].as_u64().unwrap().to_string());
    assert_eq!(mesh_headers["cache-control"], "no-store");
    let mesh_tag = format!("\"geometry-{}-{}-/map/mesh.bin\"",
        status["generation"].as_u64().unwrap(), status["revision"].as_u64().unwrap());
    assert_eq!(mesh_headers["etag"], mesh_tag.as_str());

    // If-None-Match -> 304 on every binary route.
    for path in ["/map/mesh.bin", "/map/points.bin", "/map/export.ply"] {
        let tag = format!("\"geometry-{}-{}-{path}\"",
            status["generation"].as_u64().unwrap(), status["revision"].as_u64().unwrap());
        let resp = client
            .get(format!("http://{addr}{path}"))
            .header("if-none-match", &tag)
            .send().await.unwrap();
        assert_eq!(resp.status(), 304, "{path}");
    }

    // points.bin: u32 vertex count + the mesh.bin vertex records verbatim.
    let resp = client
        .get(format!("http://{addr}/map/points.bin"))
        .send().await.unwrap();
    assert_eq!(resp.headers()["etag"], format!("\"geometry-{}-{}-/map/points.bin\"",
        status["generation"].as_u64().unwrap(), status["revision"].as_u64().unwrap()).as_str());
    let points = resp.bytes().await.unwrap();
    assert_eq!(u32::from_le_bytes(points[0..4].try_into().unwrap()), 9);
    assert_eq!(points.len(), 4 + 9 * 16);
    assert_eq!(&points[4..], &mesh_body[8..8 + 9 * 16]);

    // export.ply: binary_little_endian header, 15-byte vertices (pad byte
    // dropped), \x03-prefixed 12-byte faces, attachment disposition.
    let resp = client
        .get(format!("http://{addr}/map/export.ply"))
        .send().await.unwrap();
    assert_eq!(resp.headers()["content-disposition"],
        "attachment; filename=\"farsight-phone-mesh.ply\"");
    let ply = resp.bytes().await.unwrap();
    let header = "ply\nformat binary_little_endian 1.0\ncomment ARKit reconstructed mesh; neutral display colors\nelement vertex 9\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nelement face 3\nproperty list uchar uint vertex_indices\nend_header\n";
    assert!(ply.starts_with(header.as_bytes()), "PLY header mismatch");
    assert_eq!(ply.len(), header.len() + 9 * 15 + 3 * 13);
    for i in 0..9 {
        let rec = &ply[header.len() + i * 15..header.len() + i * 15 + 15];
        let mesh_rec = &mesh_body[8 + i * 16..8 + i * 16 + 15];
        assert_eq!(rec, mesh_rec, "vertex {i} must drop only the pad byte");
    }
    let faces = &ply[header.len() + 9 * 15..];
    for i in 0..3 {
        assert_eq!(faces[i * 13], 3, "face {i} leading vertex count");
        assert_eq!(&faces[i * 13 + 1..i * 13 + 13], &mesh_body[8 + 9 * 16 + i * 12..8 + 9 * 16 + i * 12 + 12]);
    }

    // Encoded variant still negotiates on the new routes.
    let resp = client
        .get(format!("http://{addr}/map/points.bin"))
        .header("accept-encoding", "gzip")
        .send().await.unwrap();
    if points.len() >= 1024 {
        assert_eq!(resp.headers()["content-encoding"], "gzip");
    }

    // No complete snapshot -> binary routes 503 (a fresh service with no
    // upstream), matching the puller's "Matching geometry not available".
    let shared = geom::Shared::new(10_000);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let empty_addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, geom::router(shared)).await.unwrap();
    });
    for path in ["/map/mesh.bin", "/geometry/mesh.bin", "/map/points.bin", "/map/export.ply"] {
        let resp = client
            .get(format!("http://{empty_addr}{path}"))
            .send().await.unwrap();
        assert_eq!(resp.status(), 503, "{path}");
        assert_eq!(resp.text().await.unwrap(), "Matching geometry not available");
    }
}

#[tokio::test]
async fn ws_map_streams_status_and_points() {
    use futures_util::StreamExt;
    let (addr, shared) = serve_published().await;

    // Default subscriber: JSON status each ~500ms tick plus a binary points
    // push on every published revision change.
    let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}/ws/map"))
        .await.unwrap();
    let msg = tokio::time::timeout(std::time::Duration::from_secs(2), ws.next())
        .await.unwrap().unwrap().unwrap();
    let status: serde_json::Value = serde_json::from_str(msg.to_text().unwrap()).unwrap();
    assert_eq!(status["type"], "status");
    assert_eq!(status["geometry_source"], "IPHONE ARKIT MESH");
    assert_eq!(status["triangles"], 3);
    let msg = tokio::time::timeout(std::time::Duration::from_secs(2), ws.next())
        .await.unwrap().unwrap().unwrap();
    let points = msg.into_data();
    assert_eq!(u32::from_le_bytes(points[0..4].try_into().unwrap()), 9);
    assert_eq!(points.len(), 4 + 9 * 16);

    // A new publish triggers another binary push on the next tick.
    let mut extra = upd(5, geom::Op::Replace, Some(mesh(1)));
    extra.anchor = [9; 16];
    assert!(shared.geo.lock().unwrap().apply_owned(extra));
    let mut binary_pushes = 0;
    for _ in 0..6 {
        let msg = match tokio::time::timeout(std::time::Duration::from_secs(2), ws.next()).await {
            Ok(Some(Ok(m))) => m,
            _ => break,
        };
        if msg.is_binary() {
            binary_pushes += 1;
            let points = msg.into_data();
            assert_eq!(u32::from_le_bytes(points[0..4].try_into().unwrap()), 12);
            break;
        }
    }
    assert_eq!(binary_pushes, 1, "revision change must push one points packet");
    drop(ws);

    // ?points=http subscribers receive status only.
    let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{addr}/ws/map?points=http"))
        .await.unwrap();
    for _ in 0..2 {
        let msg = tokio::time::timeout(std::time::Duration::from_secs(2), ws.next())
            .await.unwrap().unwrap().unwrap();
        assert!(msg.is_text(), "?points=http must stream status text frames only");
    }

    // No published snapshot: first push is the zero-vertex stand-in.
    let shared = geom::Shared::new(10_000);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let empty_addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, geom::router(shared)).await.unwrap();
    });
    let (mut ws, _) = tokio_tungstenite::connect_async(format!("ws://{empty_addr}/ws/map"))
        .await.unwrap();
    let _status = tokio::time::timeout(std::time::Duration::from_secs(2), ws.next())
        .await.unwrap().unwrap().unwrap();
    let msg = tokio::time::timeout(std::time::Duration::from_secs(2), ws.next())
        .await.unwrap().unwrap().unwrap();
    assert_eq!(&msg.into_data()[..], &[0, 0, 0, 0]);
}
