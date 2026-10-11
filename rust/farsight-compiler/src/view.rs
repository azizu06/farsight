//! `farsight-compile view <out-dir>`: serves the embedded three.js viewer
//! (`tools/compiler_viewer/`) plus the artefacts in `<out-dir>` so a MacBook can
//! inspect the compiled environment in a browser. The viewer UI is baked into
//! the binary — no assets directory needed at run time.

use anyhow::{Context, Result};
use std::path::{Component, Path, PathBuf};
use tiny_http::{Header, Method, Request, Response, Server};

macro_rules! asset {
    ($path:literal, $mime:literal) => {
        (
            $path,
            include_bytes!(concat!("../../../tools/compiler_viewer/", $path)) as &'static [u8],
            $mime,
        )
    };
}

/// (url path under /viewer/, bytes, content-type). Add every file the viewer loads.
static ASSETS: &[(&str, &[u8], &str)] = &[
    asset!("index.html", "text/html; charset=utf-8"),
    asset!("viewer.js", "text/javascript; charset=utf-8"),
    asset!("ply.js", "text/javascript; charset=utf-8"),
    asset!("splats.js", "text/javascript; charset=utf-8"),
    asset!("splatsort.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/three.module.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/three.core.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/loaders/GLTFLoader.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/loaders/PLYLoader.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/exporters/GLTFExporter.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/controls/OrbitControls.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/environments/RoomEnvironment.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/postprocessing/Pass.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/postprocessing/MaskPass.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/postprocessing/EffectComposer.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/postprocessing/RenderPass.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/postprocessing/ShaderPass.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/postprocessing/OutputPass.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/postprocessing/UnrealBloomPass.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/postprocessing/GTAOPass.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/postprocessing/FXAAPass.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/shaders/CopyShader.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/shaders/LuminosityHighPassShader.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/shaders/OutputShader.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/shaders/GTAOShader.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/shaders/PoissonDenoiseShader.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/shaders/FXAAShader.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/math/SimplexNoise.js", "text/javascript; charset=utf-8"),
    asset!("vendor/three/addons/utils/BufferGeometryUtils.js", "text/javascript; charset=utf-8"),
];

fn mime(name: &str) -> &'static str {
    match name.rsplit('.').next().unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js" | "mjs" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "json" => "application/json",
        "glb" => "model/gltf-binary",
        "gltf" => "model/gltf+json",
        "ply" => "application/octet-stream",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "wasm" => "application/wasm",
        _ => "application/octet-stream",
    }
}

/// Reject `..`/absolute components so `/out/` cannot escape the artefact dir.
fn sanitize(rel: &str) -> Option<PathBuf> {
    let rel = rel.strip_prefix('/').unwrap_or(rel);
    let mut p = PathBuf::new();
    for c in Path::new(rel).components() {
        match c {
            Component::Normal(s) => p.push(s),
            _ => return None,
        }
    }
    if p.as_os_str().is_empty() { None } else { Some(p) }
}

fn header(k: &str, v: &str) -> Header {
    Header::from_bytes(k.as_bytes(), v.as_bytes()).unwrap()
}

/// Serve the viewer at `http://<host>:<port>/`. Blocks until Ctrl-C.
pub fn serve(out_dir: &Path, host: &str, port: u16, open: bool) -> Result<()> {
    let out_dir = out_dir
        .canonicalize()
        .with_context(|| format!("output dir {} not found", out_dir.display()))?;
    let addr = format!("{host}:{port}");
    let server = Server::http(&addr).map_err(|e| anyhow::anyhow!("cannot bind {addr}: {e}"))?;
    let url = if host == "0.0.0.0" { format!("http://localhost:{port}/") } else { format!("http://{addr}/") };
    eprintln!("[view] serving {} at {}", out_dir.display(), url);
    eprintln!("[view] artefacts: {}", list_files(&out_dir).join(", "));
    if open { open_browser(&url); }

    for req in server.incoming_requests() {
        // One thread per request: browsers hold parallel keep-alive
        // connections, and a single-threaded accept loop stalls page loads.
        let out_dir = out_dir.clone();
        std::thread::spawn(move || handle(req, &out_dir));
    }
    Ok(())
}

fn handle(req: Request, out_dir: &Path) {
    if req.method() != &Method::Get && req.method() != &Method::Head {
        let _ = req.respond(Response::empty(405));
        return;
    }
    let path = req.url().split(['?', '#']).next().unwrap_or("/").to_string();
    if path == "/" || path == "/index.html" {
        respond_bytes(req, ASSETS[0].1, ASSETS[0].2);
    } else if let Some(rest) = path.strip_prefix("/viewer/") {
        match ASSETS.iter().find(|(p, _, _)| *p == rest) {
            Some((_, bytes, mime)) => respond_bytes(req, bytes, mime),
            None => { let _ = req.respond(Response::empty(404)); }
        }
    } else if path == "/api/manifest" {
        respond_bytes(req, &manifest(out_dir).into_bytes(), "application/json");
    } else if let Some(rest) = path.strip_prefix("/out/") {
        match sanitize(rest).map(|r| out_dir.join(r)) {
            Some(f) if f.is_file() => respond_file(req, &f, mime(rest)),
            _ => { let _ = req.respond(Response::empty(404)); }
        }
    } else {
        let _ = req.respond(Response::empty(404));
    }
}

fn respond_bytes(req: Request, bytes: &[u8], mime: &str) {
    let r = Response::from_data(bytes.to_vec())
        .with_header(header("Content-Type", mime))
        .with_header(header("Cache-Control", "no-cache"));
    let _ = req.respond(r);
}

fn respond_file(req: Request, path: &Path, mime: &str) {
    let len = path.metadata().map(|m| m.len()).unwrap_or(0);
    match std::fs::File::open(path) {
        Ok(f) => {
            let r = Response::from_file(f)
                .with_header(header("Content-Type", mime))
                .with_header(header("Content-Length", &len.to_string()))
                .with_header(header("Cache-Control", "no-cache"));
            let _ = req.respond(r);
        }
        Err(_) => { let _ = req.respond(Response::empty(404)); }
    }
}

fn list_files(dir: &Path) -> Vec<String> {
    let mut v: Vec<String> = std::fs::read_dir(dir)
        .map(|rd| {
            rd.flatten()
                .filter(|e| e.path().is_file())
                .filter_map(|e| e.file_name().into_string().ok())
                .collect()
        })
        .unwrap_or_default();
    v.sort();
    v
}

fn manifest(dir: &Path) -> String {
    let entries: Vec<String> = list_files(dir)
        .iter()
        .map(|n| {
            let sz = std::fs::metadata(dir.join(n)).map(|m| m.len()).unwrap_or(0);
            format!("{{\"name\":{},\"bytes\":{sz}}}", serde_json::to_string(n).unwrap_or_default())
        })
        .collect();
    format!(
        "{{\"dir\":{},\"files\":[{}]}}",
        serde_json::to_string(&dir.display().to_string()).unwrap_or_default(),
        entries.join(",")
    )
}

#[cfg(unix)]
fn open_browser(url: &str) {
    #[cfg(target_os = "macos")]
    let cmd = "open";
    #[cfg(not(target_os = "macos"))]
    let cmd = "xdg-open";
    let _ = std::process::Command::new(cmd)
        .arg(url)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn();
}

#[cfg(windows)]
fn open_browser(url: &str) {
    let _ = std::process::Command::new("cmd").args(["/c", "start", "", url]).spawn();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sanitize_blocks_traversal() {
        assert!(sanitize("scene.glb").is_some());
        assert!(sanitize("a/b/c.ply").is_some());
        assert!(sanitize("../evil").is_none());
        // leading "/" is re-rooted under out_dir (never absolute on disk)
        assert_eq!(sanitize("/etc/passwd").unwrap(), PathBuf::from("etc/passwd"));
        assert!(sanitize("a/../b").is_none());
        assert!(sanitize("").is_none());
        // "." components are normalized away by Path::components — harmless.
        assert_eq!(sanitize("a/./b").unwrap(), PathBuf::from("a/b"));
    }

    #[test]
    fn manifest_lists_files() {
        let d = std::env::temp_dir().join(format!("farsight-view-test-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        std::fs::write(d.join("points.ply"), b"x").unwrap();
        let m = manifest(&d);
        assert!(m.contains("points.ply"), "{m}");
        std::fs::remove_dir_all(&d).ok();
    }
}
