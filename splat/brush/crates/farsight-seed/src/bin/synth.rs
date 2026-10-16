//! farsight-seed-synth <out_dir> — write a synthetic room capture
//! (transforms.json + images/*.png + init.ply) for pipeline testing.

use anyhow::Result;
use farsight_seed::core::Rng;
use farsight_seed::synth;

fn main() -> Result<()> {
    let out = std::env::args()
        .nth(1)
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| "synth_capture".into());
    let n_views: usize = std::env::args().nth(2).and_then(|s| s.parse().ok()).unwrap_or(48);
    let n_dots: usize = std::env::args().nth(3).and_then(|s| s.parse().ok()).unwrap_or(150_000);

    let mut rng = Rng::new(0x5EED);
    let s = synth::SynthScene::default();
    let cams = synth::cameras(&s, n_views, &mut rng);
    let (pos, col) = synth::dots(&s, n_dots, &mut rng);

    std::fs::create_dir_all(out.join("images"))?;
    for (i, c) in cams.iter().enumerate() {
        let exposure = 0.85 + 0.3 * ((i as f32 * 0.37).sin() * 0.5 + 0.5);
        let img = synth::render(&s, c, exposure, &mut rng);
        img.save(out.join(format!("images/{:04}.png", i)))?;
    }
    std::fs::write(out.join("init.ply"), synth::dots_ply(&pos, &col))?;
    std::fs::write(
        out.join("transforms.json"),
        serde_json::to_vec_pretty(&synth::transforms_json(&s, &cams, n_views))?,
    )?;
    eprintln!(
        "[synth] wrote {:?}: {} views {}x{}, {} dots",
        out,
        n_views,
        s.w,
        s.h,
        pos.len()
    );
    Ok(())
}
