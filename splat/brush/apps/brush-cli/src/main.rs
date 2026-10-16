#![recursion_limit = "256"]

/// Point the cubecl persistence root (autotune cache) at a fixed dir shared
/// by every run: `CUBECL_CACHE_DIR` when set, else
/// `~/.cache/farsight-splat/cubecl`. cubecl's own default
/// (`~/Library/Caches/cubecl` on macOS, `$XDG_CACHE_HOME/cubecl` on Linux)
/// already persists, but bench harnesses expect one predictable path they
/// can clear or snapshot. `set_root` is global — call it before the first
/// kernel compiles.
#[cfg(not(target_family = "wasm"))]
fn init_cubecl_cache_root() {
    use std::path::PathBuf;
    let dir = std::env::var_os("CUBECL_CACHE_DIR")
        .map(PathBuf::from)
        .or_else(|| {
            std::env::var_os("HOME")
                .map(|h| PathBuf::from(h).join(".cache/farsight-splat/cubecl"))
        });
    if let Some(dir) = dir {
        cubecl_environment::environment::set_root(dir);
    }
}

// Headless trainer binary. The viewer lives in brush-app (the `brush` binary);
// this is a lean build of just the training path for quick CLI iteration.
#[cfg(not(target_family = "wasm"))]
fn main() -> anyhow::Result<()> {
    use brush_cli::{Cli, build_process, run_headless};
    use clap::Parser;

    init_cubecl_cache_root();
    let args = Cli::parse().validate()?;

    if args.with_viewer {
        anyhow::bail!(
            "brush-cli is headless and can't open a viewer. Pass a source to train, \
             or build the `brush` binary (brush-app) for the viewer."
        );
    }

    let runtime = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .expect("Failed to initialize tokio runtime");

    // `validate` guarantees a source is present when the viewer is off.
    let process = build_process(&args).expect("source must be present");

    runtime.block_on(run_headless(process, args.train_stream))
}

#[cfg(target_family = "wasm")]
fn main() {}
