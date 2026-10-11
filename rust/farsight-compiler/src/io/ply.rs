//! General PLY reader: ascii + binary little/big endian, arbitrary property
//! sets and orders. `x y z` required on the `vertex` element; optional
//! `nx ny nz`, `red/green/blue` (uchar or float), `alpha` (ignored),
//! `confidence`/`quality`/`weight` -> weight. Face elements are parsed for
//! `vertex_indices` lists and (optionally) sampled into the cloud via
//! `IoOptions::ply_sample_faces` + `mesh_sample_density`.
use crate::core::PointCloud;
use anyhow::{bail, Context, Result};

use super::{meshbin, IoOptions};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Ty {
    I8,
    U8,
    I16,
    U16,
    I32,
    U32,
    F32,
    F64,
}

impl Ty {
    fn from_name(s: &str) -> Option<Self> {
        Some(match s {
            "char" | "int8" => Ty::I8,
            "uchar" | "uint8" => Ty::U8,
            "short" | "int16" => Ty::I16,
            "ushort" | "uint16" => Ty::U16,
            "int" | "int32" => Ty::I32,
            "uint" | "uint32" => Ty::U32,
            "float" | "float32" => Ty::F32,
            "double" | "float64" => Ty::F64,
            _ => return None,
        })
    }
    fn size(self) -> usize {
        match self {
            Ty::I8 | Ty::U8 => 1,
            Ty::I16 | Ty::U16 => 2,
            Ty::I32 | Ty::U32 | Ty::F32 => 4,
            Ty::F64 => 8,
        }
    }
    fn is_float(self) -> bool {
        matches!(self, Ty::F32 | Ty::F64)
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Format {
    Ascii,
    BinLE,
    BinBE,
}

#[derive(Clone, Debug)]
enum Prop {
    Scalar { ty: Ty, name: String },
    List { cty: Ty, ity: Ty, name: String },
}

#[derive(Clone, Debug)]
struct Elem {
    name: String,
    count: usize,
    props: Vec<Prop>,
}

struct Header {
    format: Format,
    elems: Vec<Elem>,
    body: usize,
}

fn parse_header(bytes: &[u8]) -> Result<Header> {
    // Header is always ascii; find `end_header` line end.
    let mut pos = 0usize;
    let mut format = None;
    let mut elems: Vec<Elem> = Vec::new();
    let mut saw_ply = false;
    loop {
        let nl = bytes[pos..].iter().position(|&b| b == b'\n').ok_or_else(|| anyhow::anyhow!("PLY: unterminated header"))?;
        let line = std::str::from_utf8(&bytes[pos..pos + nl]).context("PLY: non-utf8 header")?.trim_end_matches('\r');
        let next = pos + nl + 1;
        let t: Vec<&str> = line.split_whitespace().collect();
        match t.as_slice() {
            ["ply"] => saw_ply = true,
            ["format", f, _v] => {
                format = Some(match *f {
                    "ascii" => Format::Ascii,
                    "binary_little_endian" => Format::BinLE,
                    "binary_big_endian" => Format::BinBE,
                    other => bail!("PLY: unknown format {other}"),
                });
            }
            ["element", name, c] => elems.push(Elem { name: name.to_string(), count: c.parse().context("PLY: bad element count")?, props: Vec::new() }),
            ["property", "list", ct, it, name] => {
                let Some(e) = elems.last_mut() else { bail!("PLY: property before element") };
                e.props.push(Prop::List {
                    cty: Ty::from_name(ct).ok_or_else(|| anyhow::anyhow!("PLY: bad list count type {ct}"))?,
                    ity: Ty::from_name(it).ok_or_else(|| anyhow::anyhow!("PLY: bad list item type {it}"))?,
                    name: name.to_string(),
                });
            }
            ["property", ty, name] => {
                let Some(e) = elems.last_mut() else { bail!("PLY: property before element") };
                e.props.push(Prop::Scalar {
                    ty: Ty::from_name(ty).ok_or_else(|| anyhow::anyhow!("PLY: bad scalar type {ty}"))?,
                    name: name.to_string(),
                });
            }
            ["end_header"] => {
                let format = format.ok_or_else(|| anyhow::anyhow!("PLY: no format line"))?;
                if !saw_ply {
                    bail!("PLY: missing ply magic");
                }
                return Ok(Header { format, elems, body: next });
            }
            _ => {}
        }
        pos = next;
        if pos >= bytes.len() {
            bail!("PLY: no end_header");
        }
    }
}

#[inline]
fn read_scalar(b: &[u8], o: usize, ty: Ty, be: bool) -> f64 {
    macro_rules! r {
        ($t:ty, $n:expr) => {{
            let a: [u8; $n] = b[o..o + $n].try_into().unwrap();
            if be { <$t>::from_be_bytes(a) as f64 } else { <$t>::from_le_bytes(a) as f64 }
        }};
    }
    match ty {
        Ty::I8 => b[o] as i8 as f64,
        Ty::U8 => b[o] as f64,
        Ty::I16 => r!(i16, 2),
        Ty::U16 => r!(u16, 2),
        Ty::I32 => r!(i32, 4),
        Ty::U32 => r!(u32, 4),
        Ty::F32 => r!(f32, 4),
        Ty::F64 => r!(f64, 8),
    }
}

/// Which cloud channel a vertex property feeds.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Chan {
    Skip,
    Pos(usize),
    Nrm(usize),
    Col(usize),
    Weight,
}

fn chan_of(name: &str) -> Chan {
    match name {
        "x" => Chan::Pos(0),
        "y" => Chan::Pos(1),
        "z" => Chan::Pos(2),
        "nx" | "normal_x" => Chan::Nrm(0),
        "ny" | "normal_y" => Chan::Nrm(1),
        "nz" | "normal_z" => Chan::Nrm(2),
        "red" | "r" | "diffuse_red" | "reds" => Chan::Col(0),
        "green" | "g" | "diffuse_green" | "greens" => Chan::Col(1),
        "blue" | "b" | "diffuse_blue" | "blues" => Chan::Col(2),
        "confidence" | "quality" | "weight" => Chan::Weight,
        _ => Chan::Skip,
    }
}

/// Parsed PLY body: the vertex cloud (float colour scratch kept for range
/// detection) plus any triangles read from face lists.
struct Parsed {
    cloud: PointCloud,
    /// Raw colour channels as read (byte or float); empty when no colour.
    col_raw: Vec<[f32; 3]>,
    col_float: bool,
    tris: Vec<u32>,
}

fn parse_binary(bytes: &[u8], hdr: &Header, be: bool) -> Result<Parsed> {
    let mut out = Parsed { cloud: PointCloud::default(), col_raw: Vec::new(), col_float: false, tris: Vec::new() };
    let mut cur = hdr.body;
    for elem in &hdr.elems {
        // Fixed record stride when the element has no list properties.
        let stride: Option<usize> = if elem.props.iter().all(|p| matches!(p, Prop::Scalar { .. })) {
            Some(elem.props.iter().map(|p| match p {
                Prop::Scalar { ty, .. } => ty.size(),
                Prop::List { .. } => 0,
            }).sum())
        } else {
            None
        };
        if elem.name == "vertex" {
            let n = elem.count;
            out.cloud.pos.reserve(n);
            // (offset, ty, channel) layout for scalar props.
            let mut layout: Vec<(usize, Ty, Chan)> = Vec::new();
            let mut off = 0usize;
            let mut saw = [false; 3];
            for p in &elem.props {
                match p {
                    Prop::Scalar { ty, name } => {
                        let c = chan_of(name);
                        if matches!(c, Chan::Col(_)) && ty.is_float() {
                            out.col_float = true;
                        }
                        if matches!(c, Chan::Pos(_)) {
                            saw[match c { Chan::Pos(i) => i, _ => 0 }] = true;
                        }
                        layout.push((off, *ty, c));
                        off += ty.size();
                    }
                    Prop::List { .. } => {}
                }
            }
            if !saw.iter().all(|s| *s) {
                bail!("PLY: vertex element lacks x/y/z");
            }
            let has_col = layout.iter().any(|(_, _, c)| matches!(c, Chan::Col(_)));
            let has_nrm = layout.iter().any(|(_, _, c)| matches!(c, Chan::Nrm(_)));
            let has_w = layout.iter().any(|(_, _, c)| *c == Chan::Weight);
            if has_col {
                out.col_raw.reserve(n);
            }
            if has_nrm {
                out.cloud.nrm.reserve(n);
            }
            if has_w {
                out.cloud.weight.reserve(n);
            }
            match stride {
                Some(stride) => {
                    if cur + n * stride > bytes.len() {
                        bail!("PLY: truncated vertex data");
                    }
                    for rec in bytes[cur..cur + n * stride].chunks_exact(stride) {
                        let mut p = [0f32; 3];
                        let mut nm = [0f32; 3];
                        let mut c = [0f32; 3];
                        let mut w = 1.0f32;
                        for &(o, ty, ch) in &layout {
                            let v = read_scalar(rec, o, ty, be) as f32;
                            match ch {
                                Chan::Pos(i) => p[i] = v,
                                Chan::Nrm(i) => nm[i] = v,
                                Chan::Col(i) => c[i] = v,
                                Chan::Weight => w = v,
                                Chan::Skip => {}
                            }
                        }
                        out.cloud.pos.push(p);
                        if has_col {
                            out.col_raw.push(c);
                        }
                        if has_nrm {
                            out.cloud.nrm.push(nm);
                        }
                        if has_w {
                            out.cloud.weight.push(w.max(0.0));
                        }
                    }
                    cur += n * stride;
                }
                None => {
                    // Vertex element contains a list property: walk sequentially.
                    for _ in 0..n {
                        let mut p = [0f32; 3];
                        let mut nm = [0f32; 3];
                        let mut c = [0f32; 3];
                        let mut w = 1.0f32;
                        for prop in &elem.props {
                            match prop {
                                Prop::Scalar { ty, name } => {
                                    if cur + ty.size() > bytes.len() {
                                        bail!("PLY: truncated vertex data");
                                    }
                                    let v = read_scalar(bytes, cur, *ty, be) as f32;
                                    cur += ty.size();
                                    match chan_of(name) {
                                        Chan::Pos(i) => p[i] = v,
                                        Chan::Nrm(i) => nm[i] = v,
                                        Chan::Col(i) => c[i] = v,
                                        Chan::Weight => w = v,
                                        Chan::Skip => {}
                                    }
                                }
                                Prop::List { cty, ity, .. } => {
                                    if cur + cty.size() > bytes.len() {
                                        bail!("PLY: truncated vertex list");
                                    }
                                    let cnt = read_scalar(bytes, cur, *cty, be) as usize;
                                    cur += cty.size();
                                    cur += cnt * ity.size();
                                    if cur > bytes.len() {
                                        bail!("PLY: truncated vertex list");
                                    }
                                }
                            }
                        }
                        out.cloud.pos.push(p);
                        if has_col {
                            out.col_raw.push(c);
                        }
                        if has_nrm {
                            out.cloud.nrm.push(nm);
                        }
                        if has_w {
                            out.cloud.weight.push(w.max(0.0));
                        }
                    }
                }
            }
        } else {
            // Non-vertex element: walk it; collect face index lists.
            let want_idx = elem.name == "face";
            for _ in 0..elem.count {
                let mut tri: Vec<u32> = Vec::new();
                for prop in &elem.props {
                    match prop {
                        Prop::Scalar { ty, .. } => {
                            cur += ty.size();
                            if cur > bytes.len() {
                                bail!("PLY: truncated {} element", elem.name);
                            }
                        }
                        Prop::List { cty, ity, name } => {
                            if cur + cty.size() > bytes.len() {
                                bail!("PLY: truncated {} list count", elem.name);
                            }
                            let cnt = read_scalar(bytes, cur, *cty, be) as usize;
                            cur += cty.size();
                            if cur + cnt * ity.size() > bytes.len() {
                                bail!("PLY: truncated {} list", elem.name);
                            }
                            if want_idx && (name == "vertex_indices" || name == "vertex_index" || name == "vertices") {
                                for k in 0..cnt {
                                    tri.push(read_scalar(bytes, cur + k * ity.size(), *ity, be) as u32);
                                }
                            }
                            cur += cnt * ity.size();
                        }
                    }
                }
                // Fan-decompose polygons into triangles.
                for k in 1..tri.len().saturating_sub(1) {
                    out.tris.extend_from_slice(&[tri[0], tri[k], tri[k + 1]]);
                }
            }
        }
    }
    Ok(out)
}

fn parse_ascii(bytes: &[u8], hdr: &Header) -> Result<Parsed> {
    let text = std::str::from_utf8(&bytes[hdr.body..]).context("PLY: non-utf8 ascii body")?;
    let mut lines = text.lines().map(|l| l.trim()).filter(|l| !l.is_empty());
    let mut out = Parsed { cloud: PointCloud::default(), col_raw: Vec::new(), col_float: false, tris: Vec::new() };
    for elem in &hdr.elems {
        if elem.name == "vertex" {
            let n = elem.count;
            out.cloud.pos.reserve(n);
            let chans: Vec<Chan> = elem.props.iter().map(|p| match p {
                Prop::Scalar { name, .. } => chan_of(name),
                Prop::List { .. } => Chan::Skip,
            }).collect();
            let mut saw = [false; 3];
            for (i, c) in chans.iter().enumerate() {
                if let (Chan::Pos(k), Prop::Scalar { .. }) = (*c, &elem.props[i]) {
                    saw[k] = true;
                }
            }
            if !saw.iter().all(|s| *s) {
                bail!("PLY: vertex element lacks x/y/z");
            }
            let has_col = chans.iter().any(|c| matches!(c, Chan::Col(_)));
            let has_nrm = chans.iter().any(|c| matches!(c, Chan::Nrm(_)));
            let has_w = chans.contains(&Chan::Weight);
            if elem.props.iter().any(|p| matches!(p, Prop::Scalar { ty, name } if ty.is_float() && matches!(chan_of(name), Chan::Col(_)))) {
                out.col_float = true;
            }
            if has_col {
                out.col_raw.reserve(n);
            }
            if has_nrm {
                out.cloud.nrm.reserve(n);
            }
            if has_w {
                out.cloud.weight.reserve(n);
            }
            for _ in 0..n {
                let line = lines.next().ok_or_else(|| anyhow::anyhow!("PLY: short ascii body"))?;
                let mut toks = line.split_whitespace();
                let mut p = [0f32; 3];
                let mut nm = [0f32; 3];
                let mut c = [0f32; 3];
                let mut w = 1.0f32;
                for (i, prop) in elem.props.iter().enumerate() {
                    match prop {
                        Prop::Scalar { .. } => {
                            let v: f32 = toks.next().ok_or_else(|| anyhow::anyhow!("PLY: short row"))?.parse().context("PLY: bad number")?;
                            match chans[i] {
                                Chan::Pos(k) => p[k] = v,
                                Chan::Nrm(k) => nm[k] = v,
                                Chan::Col(k) => c[k] = v,
                                Chan::Weight => w = v,
                                Chan::Skip => {}
                            }
                        }
                        Prop::List { .. } => {
                            let cnt: usize = toks.next().ok_or_else(|| anyhow::anyhow!("PLY: short row"))?.parse().context("PLY: bad list count")?;
                            for _ in 0..cnt {
                                toks.next();
                            }
                        }
                    }
                }
                out.cloud.pos.push(p);
                if has_col {
                    out.col_raw.push(c);
                }
                if has_nrm {
                    out.cloud.nrm.push(nm);
                }
                if has_w {
                    out.cloud.weight.push(w.max(0.0));
                }
            }
        } else {
            let want_idx = elem.name == "face";
            for _ in 0..elem.count {
                let line = lines.next().ok_or_else(|| anyhow::anyhow!("PLY: short ascii body"))?;
                let mut toks = line.split_whitespace();
                let mut tri: Vec<u32> = Vec::new();
                for prop in &elem.props {
                    match prop {
                        Prop::Scalar { .. } => {
                            toks.next();
                        }
                        Prop::List { name, .. } => {
                            let cnt: usize = toks.next().ok_or_else(|| anyhow::anyhow!("PLY: short row"))?.parse().context("PLY: bad list count")?;
                            if want_idx && (name == "vertex_indices" || name == "vertex_index" || name == "vertices") {
                                for _ in 0..cnt {
                                    if let Some(t) = toks.next() {
                                        tri.push(t.parse().context("PLY: bad index")?);
                                    }
                                }
                            } else {
                                for _ in 0..cnt {
                                    toks.next();
                                }
                            }
                        }
                    }
                }
                for k in 1..tri.len().saturating_sub(1) {
                    out.tris.extend_from_slice(&[tri[0], tri[k], tri[k + 1]]);
                }
            }
        }
    }
    Ok(out)
}

fn finish(mut p: Parsed, opts: &IoOptions) -> PointCloud {
    // Colour channel: uchar data is already 0-255; float data is 0-1 (rescale)
    // unless it clearly carries byte-range values.
    if !p.col_raw.is_empty() {
        let n = p.cloud.pos.len();
        let mut col = Vec::with_capacity(n);
        let max = p.col_raw.iter().flat_map(|c| c.iter().copied()).fold(0f32, f32::max);
        let scale = if p.col_float && max <= 1.000001 { 255.0 } else { 1.0 };
        for c in &p.col_raw {
            col.push([
                (c[0] * scale).clamp(0.0, 255.0) as u8,
                (c[1] * scale).clamp(0.0, 255.0) as u8,
                (c[2] * scale).clamp(0.0, 255.0) as u8,
            ]);
        }
        p.cloud.col = col;
    }
    if opts.ply_sample_faces && opts.mesh_sample_density > 0.0 && !p.tris.is_empty() {
        let extra = meshbin::sample_faces(&p.cloud.pos, &p.cloud.col, &p.tris, opts.mesh_sample_density);
        p.cloud.pos.extend(extra.pos);
        p.cloud.col.extend(extra.col);
        if !p.cloud.nrm.is_empty() {
            p.cloud.nrm.resize(p.cloud.pos.len(), [0.0; 3]);
        }
        if !p.cloud.weight.is_empty() {
            p.cloud.weight.resize(p.cloud.pos.len(), 1.0);
        }
    }
    p.cloud
}

/// Parse a PLY body with explicit options.
pub fn read_points_opts(bytes: &[u8], opts: &IoOptions) -> Result<PointCloud> {
    let hdr = parse_header(bytes)?;
    let parsed = match hdr.format {
        Format::Ascii => parse_ascii(bytes, &hdr)?,
        Format::BinLE => parse_binary(bytes, &hdr, false)?,
        Format::BinBE => parse_binary(bytes, &hdr, true)?,
    };
    Ok(finish(parsed, opts))
}

/// Parse a PLY body (default options).
pub fn read_points(bytes: &[u8]) -> Result<PointCloud> {
    read_points_opts(bytes, &IoOptions::default())
}
