//! glTF 2.0 binary (.glb) export: POSITION/NORMAL/COLOR_0/TEXCOORD_0/_SUPPORT
//! vertex data, u32 indices, PBR metallic-roughness material with embedded
//! baseColor texture, and compile metadata in asset.extras.

use crate::core::{Image, Mesh};
use anyhow::{Context, Result};

fn pad4(v: &mut Vec<u8>, byte: u8) {
    while v.len() % 4 != 0 {
        v.push(byte);
    }
}

fn png_bytes(img: &Image) -> Result<Vec<u8>> {
    let mut buf = std::io::Cursor::new(Vec::new());
    image::RgbImage::from_raw(img.width, img.height, img.rgb.clone())
        .context("bad texture image")?
        .write_to(&mut buf, image::ImageFormat::Png)?;
    Ok(buf.into_inner())
}

/// Serialize the mesh (+ optional texture) to a .glb binary.
/// `extras` is merged into asset.extras (compile report fields).
pub fn scene(mesh: &Mesh, extras: serde_json::Value) -> Result<Vec<u8>> {
    let mut bin: Vec<u8> = Vec::new();
    let mut views: Vec<serde_json::Value> = Vec::new();
    let mut accessors: Vec<serde_json::Value> = Vec::new();

    let mut push = |data: &[u8], target: Option<u32>, comp: u32, ctype: &str, count: usize, minmax: Option<([f32; 3], [f32; 3])>, normalized: bool| {
        pad4(&mut bin, 0);
        let offset = bin.len();
        bin.extend_from_slice(data);
        views.push(serde_json::json!({ "buffer": 0, "byteOffset": offset, "byteLength": data.len(), "target": target }));
        let mut a = serde_json::json!({
            "bufferView": views.len() - 1, "componentType": comp, "count": count, "type": ctype,
        });
        if normalized {
            a["normalized"] = serde_json::json!(true);
        }
        if let Some((mn, mx)) = minmax {
            a["min"] = serde_json::json!(mn);
            a["max"] = serde_json::json!(mx);
        }
        accessors.push(a);
        accessors.len() - 1
    };

    // POSITION (required min/max for viewers)
    let mut mn = [f32::INFINITY; 3];
    let mut mx = [f32::NEG_INFINITY; 3];
    let mut posb = Vec::with_capacity(mesh.pos.len() * 12);
    for p in &mesh.pos {
        for k in 0..3 {
            mn[k] = mn[k].min(p[k]);
            mx[k] = mx[k].max(p[k]);
            posb.extend_from_slice(&p[k].to_le_bytes());
        }
    }
    let a_pos = push(&posb, Some(34962), 5126, "VEC3", mesh.pos.len(), Some((mn, mx)), false);

    let mut attrs = serde_json::json!({ "POSITION": a_pos });

    if mesh.nrm.len() == mesh.pos.len() {
        let mut b = Vec::with_capacity(mesh.nrm.len() * 12);
        for n in &mesh.nrm {
            for v in n {
                b.extend_from_slice(&v.to_le_bytes());
            }
        }
        attrs["NORMAL"] = serde_json::json!(push(&b, Some(34962), 5126, "VEC3", mesh.nrm.len(), None, false));
    }
    if mesh.col.len() == mesh.pos.len() {
        let mut b = Vec::with_capacity(mesh.col.len() * 4);
        for c in &mesh.col {
            b.extend_from_slice(&[c[0], c[1], c[2], 255]);
        }
        attrs["COLOR_0"] = serde_json::json!(push(&b, Some(34962), 5121, "VEC4", mesh.col.len(), None, true));
    }
    if mesh.uv.len() == mesh.pos.len() {
        let mut b = Vec::with_capacity(mesh.uv.len() * 8);
        for uv in &mesh.uv {
            b.extend_from_slice(&uv[0].to_le_bytes());
            b.extend_from_slice(&uv[1].to_le_bytes());
        }
        attrs["TEXCOORD_0"] = serde_json::json!(push(&b, Some(34962), 5126, "VEC2", mesh.uv.len(), None, false));
    }
    if mesh.support.len() == mesh.pos.len() {
        let mut b = Vec::with_capacity(mesh.support.len() * 4);
        for s in &mesh.support {
            b.extend_from_slice(&s.to_le_bytes());
        }
        attrs["_SUPPORT"] = serde_json::json!(push(&b, Some(34962), 5126, "SCALAR", mesh.support.len(), None, false));
    }
    let a_idx = {
        let mut b = Vec::with_capacity(mesh.idx.len() * 4);
        for i in &mesh.idx {
            b.extend_from_slice(&i.to_le_bytes());
        }
        push(&b, Some(34963), 5125, "SCALAR", mesh.idx.len(), None, false)
    };

    // material + optional texture
    let mut material = serde_json::json!({
        "name": "farsight-compiled",
        "pbrMetallicRoughness": { "metallicFactor": 0.0, "roughnessFactor": 0.85 },
        "doubleSided": true,
    });
    let mut images_j = serde_json::json!([]);
    let mut textures_j = serde_json::json!([]);
    let mut samplers_j = serde_json::json!([]);
    if let Some(tex) = &mesh.texture {
        let png = png_bytes(tex)?;
        pad4(&mut bin, 0);
        let off = bin.len();
        bin.extend_from_slice(&png);
        views.push(serde_json::json!({ "buffer": 0, "byteOffset": off, "byteLength": png.len() }));
        images_j = serde_json::json!([{ "mimeType": "image/png", "bufferView": views.len() - 1 }]);
        samplers_j = serde_json::json!([{ "magFilter": 9729, "minFilter": 9987, "wrapS": 33071, "wrapT": 33071 }]);
        textures_j = serde_json::json!([{ "source": 0, "sampler": 0 }]);
        material["pbrMetallicRoughness"]["baseColorTexture"] = serde_json::json!({ "index": 0 });
    }

    let mut asset = serde_json::json!({ "version": "2.0", "generator": "farsight-compiler", "extras": extras });
    if !mesh.support.is_empty() {
        asset["extras"]["_SUPPORT"] = serde_json::json!("metres from vertex to nearest measured sample");
    }
    let mut doc = serde_json::json!({
        "asset": asset,
        "scene": 0,
        "scenes": [{ "nodes": [0] }],
        "nodes": [{ "mesh": 0, "name": "environment" }],
        "meshes": [{ "name": "environment", "primitives": [{ "attributes": attrs, "indices": a_idx, "material": 0, "mode": 4 }] }],
        "materials": [material],
        "bufferViews": views,
        "accessors": accessors,
        "buffers": [{ "byteLength": bin.len() }],
    });
    if !images_j.as_array().unwrap().is_empty() {
        doc["images"] = images_j;
        doc["textures"] = textures_j;
        doc["samplers"] = samplers_j;
    }

    let mut json = serde_json::to_vec(&doc)?;
    pad4(&mut json, b' ');
    pad4(&mut bin, 0);
    let total = 12 + 8 + json.len() + 8 + bin.len();
    let mut out = Vec::with_capacity(total);
    out.extend_from_slice(b"glTF");
    out.extend_from_slice(&2u32.to_le_bytes());
    out.extend_from_slice(&(total as u32).to_le_bytes());
    out.extend_from_slice(&(json.len() as u32).to_le_bytes());
    out.extend_from_slice(b"JSON");
    out.extend_from_slice(&json);
    out.extend_from_slice(&(bin.len() as u32).to_le_bytes());
    out.extend_from_slice(b"BIN\0");
    out.extend_from_slice(&bin);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The GLB must parse as a container: magic, two chunks, valid JSON doc,
    /// accessor counts matching the mesh.
    #[test]
    fn glb_roundtrip() {
        let mesh = Mesh {
            pos: vec![[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            nrm: vec![[0.0, 0.0, 1.0]; 3],
            col: vec![[1, 2, 3]; 3],
            uv: vec![[0.0, 0.0], [1.0, 0.0], [0.0, 1.0]],
            support: vec![0.0, 0.1, 0.2],
            idx: vec![0, 1, 2],
            texture: Some(Image { width: 2, height: 2, rgb: vec![128; 12] }),
        };
        let glb = scene(&mesh, serde_json::json!({"test": 1})).unwrap();
        assert_eq!(&glb[0..4], b"glTF");
        assert_eq!(&glb[16..20], b"JSON");
        let jlen = u32::from_le_bytes(glb[12..16].try_into().unwrap()) as usize;
        let doc: serde_json::Value = serde_json::from_slice(&glb[20..20 + jlen]).unwrap();
        assert_eq!(doc["accessors"].as_array().unwrap().len(), 6); // pos nrm col uv support idx
        assert_eq!(doc["images"][0]["mimeType"], "image/png");
        assert_eq!(doc["meshes"][0]["primitives"][0]["attributes"]["_SUPPORT"], 4);
        let blen = u32::from_le_bytes(glb[20 + jlen..24 + jlen].try_into().unwrap()) as usize;
        assert_eq!(20 + jlen + 8 + blen, glb.len());
    }
}
