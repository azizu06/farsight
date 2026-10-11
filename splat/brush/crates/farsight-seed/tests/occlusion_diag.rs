//! Diagnostic: in-memory synth capture -> colour::project coverage rates at
//! varying occ_eps / depth_buf_scale / dilate. Run:
//!   cargo test -p farsight-seed --test occlusion_diag -- --nocapture

use farsight_seed::core::Rng;
use farsight_seed::{colour, synth, views::SeedView};
use glam::Vec3;

#[test]
fn coverage_vs_eps() {
    let mut rng = Rng::new(0x5EED);
    let s = synth::SynthScene::default();
    let cams = synth::cameras(&s, 24, &mut rng);
    let (pos, _col) = synth::dots(&s, 60_000, &mut rng);
    let views: Vec<SeedView> = cams
        .iter()
        .enumerate()
        .map(|(i, c)| SeedView {
            camera: c.clone(),
            frame_index: i,
            file_path: String::new(),
            image: None,
        })
        .collect();

    for &(ds, dil) in &[(2.0f32, 1u32), (4.0, 2), (1.0, 0)] {
        for &eps in &[0.02f32, 0.04, 0.08, 0.15] {
            let cfg = colour::ColourConfig {
                depth_buf_scale: ds,
                dilate: dil,
                occ_eps: eps,
                ..Default::default()
            };
            let r = colour::project(&pos, &views, &cfg, 1, usize::MAX);
            let (mut i_, mut a_) = (0u64, 0u64);
            for &(_, n_in, n_ok) in &r.view_stats {
                i_ += n_in as u64;
                a_ += n_ok as u64;
            }
            let cov = r.colours.iter().filter(|c| c.is_some()).count();
            println!(
                "ds={ds} dilate={dil} eps={eps}: in={i_} ok={a_} ({:.1}%) points_cov={:.1}%",
                100.0 * a_ as f64 / i_.max(1) as f64,
                100.0 * cov as f64 / pos.len() as f64
            );
        }
    }
}
