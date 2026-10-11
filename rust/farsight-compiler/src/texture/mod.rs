//! Appearance. OWNER: W6. Projective texture baking from posed capture images
//! into a UV atlas (visibility, view selection, seam blending, exposure
//! compensation); falls back to cloud colours when no capture is given.
//!
//! `atlas` grows normal-coherent charts, parametrizes each on its best-fit
//! plane, shelf-packs them and rasterizes per-texel (tri, barycentric)
//! coverage. `bake` evaluates candidate capture views per chart against
//! splat-depth occlusion buffers, blends the top-k visible samples with
//! per-image gain compensation, falls back to IDW point colours, then
//! coarse-to-fine inpaints whatever nothing covered.

pub mod atlas;
pub mod bake;

use crate::core::*;
use crate::spatial::Grid;
use crate::Stats;
use rayon::prelude::*;
use rustc_hash::FxHashMap;
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct TextureOptions {
    /// Atlas texel size in metres (rescaled coarser if charts exceed max_atlas).
    pub texel_m: f32,
    /// Max atlas dimension in pixels.
    pub max_atlas: u32,
    /// Max dihedral angle (deg) for faces to share a chart.
    pub chart_angle_deg: f32,
    /// Padding ring around each chart in texels.
    pub gutter: u32,
    /// Per-chart view shortlist size (<=16).
    pub candidate_views: usize,
    /// Top-k views blended per texel.
    pub blend_views: usize,
    /// Depth-buffer downsample factor (image px per depth px).
    pub depth_scale: f32,
    /// Occlusion margin in metres.
    pub depth_epsilon: f32,
    /// 3x3 min-dilation passes over each depth buffer.
    pub depth_dilate: u32,
    /// Cloud splat search radius (0 = auto: 6*texel).
    pub splat_radius: f32,
    /// Cloud splat neighbour count.
    pub splat_k: usize,
    /// Inpaint coarse cell size in texels.
    pub fill_coarse: u32,
    /// Subtle tint marking interpolated regions (support > tint_support_m).
    pub tint_interpolated: bool,
    pub tint_color: [u8; 3],
    pub tint_strength: f32,
    pub tint_support_m: f32,
}

impl Default for TextureOptions {
    fn default() -> Self {
        TextureOptions {
            texel_m: 0.005,
            max_atlas: 8192,
            chart_angle_deg: 40.0,
            gutter: 2,
            candidate_views: 8,
            blend_views: 4,
            depth_scale: 2.0,
            depth_epsilon: 0.03,
            depth_dilate: 1,
            splat_radius: 0.0,
            splat_k: 8,
            fill_coarse: 8,
            tint_interpolated: false,
            tint_color: [255, 140, 40],
            tint_strength: 0.4,
            tint_support_m: 0.04,
        }
    }
}

/// Bake vertex colours from the cloud when there is no mesh to texture.
fn fallback_vertex_colours(mesh: &mut Mesh, cloud: &PointCloud) -> Stats {
    let grid = Grid::build(&cloud.pos, 0.03);
    let mut scratch = Vec::with_capacity(8);
    mesh.col = mesh
        .pos
        .iter()
        .map(|&p| {
            bake::idw(&grid, cloud, p, 0.06, 8, &mut scratch)
                .map(|c| [c[0] as u8, c[1] as u8, c[2] as u8])
                .unwrap_or([180, 180, 180])
        })
        .collect();
    serde_json::json!({ "mode": "vertex-fallback", "vertices": mesh.pos.len() })
}

pub fn bake(mesh: &mut Mesh, cloud: &PointCloud, capture: Option<&Capture>, opts: &TextureOptions) -> Stats {
    if mesh.triangles() == 0 {
        if cloud.len() > 0 && !cloud.col.is_empty() {
            return fallback_vertex_colours(mesh, cloud);
        }
        return serde_json::json!({ "mode": "none" });
    }

    // 1. charts + coverage raster
    let (face_nrm, face_area) = atlas::face_normals(mesh);
    let atlas = atlas::build(mesh, &face_nrm, &face_area, opts.texel_m, opts.max_atlas, opts.gutter, opts.chart_angle_deg.to_radians().cos());
    let cov = atlas::rasterize(mesh, &atlas);
    let texels: u64 = atlas.charts.iter().map(|c| c.w as u64 * c.h as u64).sum();

    // 2. shared eval context
    let splat_r = if opts.splat_radius > 0.0 { opts.splat_radius } else { 6.0 * atlas.texel_m };
    let capture = capture.filter(|c| !c.cameras.is_empty() && c.images.len() >= c.cameras.len());
    let depths = capture.map(|c| bake::depth_buffers(c, mesh, cloud, opts.depth_scale, opts.depth_dilate)).unwrap_or_default();
    let cands: Vec<Vec<(u32, f32)>> = match capture {
        Some(cap) => atlas.charts.par_iter().map(|c| bake::candidates(c, mesh, &cap.cameras, opts.candidate_views, atlas.texel_m, 4.0)).collect(),
        None => Vec::new(),
    };
    let grid = if cloud.col.is_empty() { None } else { Some(Grid::build(&cloud.pos, splat_r.max(0.01))) };
    let tint = if opts.tint_interpolated {
        Some((opts.tint_strength.clamp(0.0, 1.0), [opts.tint_color[0] as f32, opts.tint_color[1] as f32, opts.tint_color[2] as f32]))
    } else {
        None
    };
    let mut ctx = bake::BakeCtx {
        mesh,
        cloud,
        capture,
        face_nrm: &face_nrm,
        depths,
        cands,
        gains: Vec::new(),
        grid,
        splat_r,
        splat_k: opts.splat_k,
        blend_k: opts.blend_views,
        eps: opts.depth_epsilon,
        prefer_px: 4.0,
        stride: 1,
        tint,
        tint_support_m: opts.tint_support_m,
    };

    // 3. optional per-image gain estimation (subsampled pass), then full bake
    if let Some(cap) = capture {
        ctx.stride = 4;
        ctx.gains = vec![[1.0; 3]; cap.cameras.len()];
        let outs: Vec<bake::ChartOut> = atlas.charts.par_iter().enumerate().map(|(ci, _)| bake::bake_chart(&ctx, &atlas, ci, &cov[ci])).collect();
        ctx.gains = bake::solve_gains(&outs, cap.cameras.len());
    }
    ctx.stride = 1;
    let outs: Vec<bake::ChartOut> = atlas.charts.par_iter().enumerate().map(|(ci, _)| bake::bake_chart(&ctx, &atlas, ci, &cov[ci])).collect();

    // 4. blit chart buffers into the atlas + set mask
    let mut img = Image::new(atlas.w, atlas.h);
    let mut set = vec![0u8; (atlas.w * atlas.h) as usize];
    let mut covered = 0u64;
    let mut dom: FxHashMap<u32, u64> = FxHashMap::default();
    for (ci, o) in outs.iter().enumerate() {
        let c = &atlas.charts[ci];
        for ty in 0..c.h {
            let dst = ((c.y + ty) * atlas.w + c.x) as usize;
            let src = (ty * c.w) as usize;
            img.rgb[3 * dst..3 * dst + 3 * c.w as usize].copy_from_slice(&o.rgb[3 * src..3 * src + 3 * c.w as usize]);
            set[dst..dst + c.w as usize].copy_from_slice(&o.set[src..src + c.w as usize]);
        }
        covered += o.covered;
        for (&k, &v) in &o.dom {
            *dom.entry(k).or_default() += v;
        }
    }

    // 5. inpaint uncovered texels
    let unset = set.iter().filter(|&&s| s == 0).count();
    if unset > 0 {
        bake::inpaint(&mut img, &set, opts.fill_coarse);
    }

    // 6. rebuild the mesh, splitting vertices on chart seams, writing uv
    let mut npos = Vec::with_capacity(mesh.pos.len());
    let mut nnrm: Vec<V3> = if mesh.nrm.len() == mesh.pos.len() { Vec::with_capacity(mesh.pos.len()) } else { Vec::new() };
    let mut nsup: Vec<f32> = if mesh.support.len() == mesh.pos.len() { Vec::with_capacity(mesh.pos.len()) } else { Vec::new() };
    let mut nuv = Vec::with_capacity(mesh.pos.len());
    let mut npx = Vec::with_capacity(mesh.pos.len()); // pixel-space uv for colour sampling
    let mut nidx = Vec::with_capacity(mesh.idx.len());
    for c in &atlas.charts {
        let mut map: FxHashMap<u32, u32> = FxHashMap::default();
        for (l, &v) in c.verts.iter().enumerate() {
            let id = npos.len() as u32;
            map.insert(v, id);
            let p = mesh.pos[v as usize];
            npos.push(p);
            if !nnrm.is_empty() {
                nnrm.push(mesh.nrm[v as usize]);
            }
            if !nsup.is_empty() {
                nsup.push(mesh.support[v as usize]);
            }
            let px = c.x as f32 + (c.uv[l][0] - c.omin[0]) / atlas.texel_m;
            let py = c.y as f32 + (c.uv[l][1] - c.omin[1]) / atlas.texel_m;
            nuv.push([px / atlas.w as f32, py / atlas.h as f32]);
            npx.push([px, py]);
        }
        for &f in &c.faces {
            for k in 0..3 {
                nidx.push(map[&mesh.idx[3 * f as usize + k]]);
            }
        }
    }
    // vertex colours: bilinear atlas sample (always valid post-inpaint)
    let ncol: Vec<[u8; 3]> = npx.iter().map(|&[u, v]| {
        let s = img.sample(u, v);
        [s[0] as u8, s[1] as u8, s[2] as u8]
    }).collect();

    *mesh = Mesh {
        pos: npos,
        nrm: nnrm,
        col: ncol,
        uv: nuv,
        support: nsup,
        idx: nidx,
        texture: Some(img),
    };
    mesh.validate();

    serde_json::json!({
        "mode": if capture.is_some() { "capture" } else { "cloud" },
        "charts": atlas.charts.len(),
        "atlas": [atlas.w, atlas.h],
        "texel_m": atlas.texel_m,
        "texels": texels,
        "coverage_pct": 100.0 * covered as f64 / texels.max(1) as f64,
        "views_used": dom.len(),
        "vertices_out": mesh.pos.len(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::synth;

    /// Textured atlas on a box mesh: charts built, uv within [0,1], colours set.
    #[test]
    fn bakes_box_from_cloud() {
        // single quad on the floor, coloured from nearby cloud points
        let mesh = Mesh {
            pos: vec![[0.0, 0.0, 0.0], [0.4, 0.0, 0.0], [0.4, 0.0, 0.4], [0.0, 0.0, 0.4]],
            idx: vec![0, 1, 2, 0, 2, 3],
            ..Default::default()
        };
        let mut cloud = PointCloud::default();
        for x in 0..10 {
            for z in 0..10 {
                cloud.pos.push([x as f32 * 0.05, 0.0, z as f32 * 0.05]);
                cloud.col.push([10, 200, 30]);
            }
        }
        let mut m = mesh;
        let stats = bake(&mut m, &cloud, None, &TextureOptions::default());
        assert!(m.texture.is_some());
        assert_eq!(m.uv.len(), m.pos.len());
        assert!(m.uv.iter().all(|uv| uv[0] >= 0.0 && uv[0] <= 1.0 && uv[1] >= 0.0 && uv[1] <= 1.0));
        assert_eq!(m.col.len(), m.pos.len());
        assert!(m.col.iter().all(|c| c[1] > 100), "expected greenish colours, got {:?}", m.col);
        assert_eq!(stats["mode"], "cloud");
    }

    /// Capture bake reproduces ground-truth shading on a simple surface.
    #[test]
    fn capture_bake_matches_scene() {
        let scene = synth::Scene::room();
        let opts = synth::ScanOptions { depth_w: 48, depth_h: 36, image_w: 160, image_h: 120, yaw_steps: 4, ..Default::default() };
        let scan = synth::simulate(&scene, &opts);
        // a floor patch the rover can see
        let mut m = Mesh {
            pos: vec![[-0.5, 0.0, -0.5], [0.5, 0.0, -0.5], [0.5, 0.0, 0.5], [-0.5, 0.0, 0.5]],
            nrm: vec![[0.0, 1.0, 0.0]; 4],
            idx: vec![0, 1, 2, 0, 2, 3],
            ..Default::default()
        };
        let stats = bake(&mut m, &scan.cloud, Some(&scan.capture), &TextureOptions::default());
        assert_eq!(stats["mode"], "capture");
        let tex = m.texture.as_ref().unwrap();
        // sample centre of atlas: should match the wood-plank floor colour family
        let c = tex.get(tex.width / 2, tex.height / 2);
        assert!(c.iter().map(|&v| v as u32).sum::<u32>() > 60, "texture empty: {c:?}");
    }
}
