//! Port of jetson/tests/test_identity_{engine,service,server}.py:
//! admission pixel thresholds, cosine thresholds/margin, expiry,
//! heartbeat cadence, Unknown shaping, thumbnail bounds, consent/413/422
//! rejections, gallery persistence + concurrency, busy-enroll semantics,
//! generation invalidation and model-startup failure semantics.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use farsight_server::camera::{CameraFrame, CameraService};
use farsight_server::identity::{
    image_dimensions, DetectedFace, FaceDetect, FaceRecognize, Gallery, IdentityEngine,
    IdentityInfer, IdentityService, FACE_ROW,
};
use farsight_server::state::{now_monotonic, MapState};
use base64::Engine;
use image::ImageEncoder;
use serde_json::{json, Value};

// ---------- helpers ----------

/// test_identity_engine.vector: basis index or a 2-vector with a fixed cosine
/// against e0.
fn vector(index: usize, cosine: Option<f32>) -> Vec<f32> {
    let mut v = vec![0f32; 128];
    match cosine {
        None => v[index] = 1.0,
        Some(c) => {
            v[0] = c;
            v[1] = (1.0 - c * c).sqrt();
        }
    }
    v
}

fn feature(index: usize) -> Vec<f32> {
    vector(index, None)
}

/// test_identity_engine.face: box + five landmarks + confidence.
fn face(x: f32, y: f32, width: f32, height: f32, conf: f32) -> [f32; FACE_ROW] {
    [
        x, y, width, height, x + 30.0, y + 35.0, x + 70.0, y + 35.0, x + 50.0, y + 55.0,
        x + 35.0, y + 80.0, x + 65.0, y + 80.0, conf,
    ]
}

fn jpeg(width: u32, height: u32) -> Vec<u8> {
    let image = image::RgbImage::new(width, height);
    let mut buf = Vec::new();
    image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, 90)
        .write_image(image.as_raw(), width, height, image::ExtendedColorType::Rgb8)
        .unwrap();
    buf
}

fn png(width: u32, height: u32) -> Vec<u8> {
    let image = image::RgbImage::new(width, height);
    let mut buf = Vec::new();
    image::codecs::png::PngEncoder::new(&mut buf)
        .write_image(image.as_raw(), width, height, image::ExtendedColorType::Rgb8)
        .unwrap();
    buf
}

struct FakeDetect {
    rows: Vec<[f32; FACE_ROW]>,
    dims: Arc<Mutex<(u32, u32)>>,
}

impl FaceDetect for FakeDetect {
    fn set_input_size(&mut self, width: u32, height: u32) {
        *self.dims.lock().unwrap() = (width, height);
    }
    fn detect(&mut self, _image: &image::RgbImage) -> Result<Vec<[f32; FACE_ROW]>, String> {
        Ok(self.rows.clone())
    }
}

struct FakeRecognize {
    embedding: Vec<f32>,
    /// None mirrors `image[0:112, 0:112]`; Some overrides the aligned crop.
    aligned: Option<image::RgbImage>,
}

impl FaceRecognize for FakeRecognize {
    fn align_crop(&mut self, image: &image::RgbImage, _row: &[f32; FACE_ROW]) -> Result<image::RgbImage, String> {
        Ok(match &self.aligned {
            Some(a) => a.clone(),
            None => image::imageops::crop_imm(image, 0, 0, 112, 112).to_image(),
        })
    }
    fn feature(&mut self, _aligned: &image::RgbImage) -> Result<Vec<f32>, String> {
        Ok(self.embedding.clone())
    }
}

fn engine(rows: Vec<[f32; FACE_ROW]>, embedding: Vec<f32>) -> IdentityEngine {
    engine_with_aligned(rows, embedding, None)
}

fn engine_with_aligned(
    rows: Vec<[f32; FACE_ROW]>,
    embedding: Vec<f32>,
    aligned: Option<image::RgbImage>,
) -> IdentityEngine {
    IdentityEngine::new(
        Box::new(FakeDetect { rows, dims: Arc::new(Mutex::new((640, 480))) }),
        Box::new(FakeRecognize { embedding, aligned }),
    )
}

// ---------- gallery (test_identity_engine.GalleryTests) ----------

#[test]
fn gallery_unknown_and_clear_match_do_not_claim_probability() {
    let gallery = Gallery::new(None).unwrap();
    assert!(gallery.match_person(&vector(0, None)).is_none());
    let scaled: Vec<f32> = vector(0, None).iter().map(|v| v * 4.0).collect();
    let person = gallery.enroll("  Mehdi  ", &scaled, None).unwrap();
    assert_eq!(person["name"], "Mehdi");
    assert_eq!(person["samples"], 1);
    let m = gallery.match_person(&vector(0, None)).unwrap();
    assert_eq!(m["person_id"], person["id"]);
    assert_eq!(m["name"], "Mehdi");
    assert!((m["similarity"].as_f64().unwrap() - 1.0).abs() < 1e-6);
    assert!(gallery.match_person(&vector(2, None)).is_none());
    assert!(m.get("confidence").is_none());
}

#[test]
fn gallery_two_similar_people_are_unknown_instead_of_wrong_name() {
    let gallery = Gallery::new(None).unwrap();
    gallery.enroll("One", &vector(0, None), None).unwrap();
    gallery.enroll("Two", &vector(0, Some(0.98)), None).unwrap();
    assert!(gallery.match_person(&vector(0, None)).is_none());
}

#[test]
fn gallery_samples_and_delete_invalidate_generation() {
    let gallery = Gallery::with_limits(None, 20, 2, 0.5, 0.08).unwrap();
    let person = gallery.enroll("One", &vector(0, None), None).unwrap();
    assert_eq!(gallery.generation(), 1);
    let id = person["id"].as_str().unwrap().to_string();
    let again = gallery.enroll("One", &vector(0, Some(0.95)), Some(&id)).unwrap();
    assert_eq!(again["samples"], 2);
    assert_eq!(gallery.generation(), 2);
    assert!(gallery.enroll("One", &vector(0, None), Some(&id)).is_err());
    assert_eq!(gallery.generation(), 2);
    assert!(gallery.remove(&id).unwrap());
    assert!(gallery.match_person(&vector(0, None)).is_none());
    assert_eq!(gallery.generation(), 3);
    assert!(!gallery.remove(&id).unwrap());
    assert_eq!(gallery.generation(), 3);
}

#[test]
fn gallery_rejects_invalid_name_vectors_and_capacity_without_mutating() {
    let gallery = Gallery::with_limits(None, 1, 5, 0.5, 0.08).unwrap();
    assert!(gallery.enroll("", &vector(0, None), None).is_err());
    assert!(gallery.enroll(&"x".repeat(49), &vector(0, None), None).is_err());
    assert!(gallery.enroll("x\nnext", &vector(0, None), None).is_err());
    assert!(gallery.enroll("x", &vec![0.0; 128], None).is_err());
    assert!(gallery.enroll("x", &vec![1.0; 127], None).is_err());
    assert!(gallery.enroll("x", &vec![f32::NAN; 128], None).is_err());
    assert_eq!(gallery.people(), Vec::<Value>::new());
    gallery.enroll("One", &vector(0, None), None).unwrap();
    assert!(gallery.enroll("Two", &vector(1, None), None).is_err());
    assert_eq!(gallery.people().len(), 1);
    assert!(gallery.enroll("One", &vector(0, None), Some("unknown-id")).is_err());
}

#[test]
fn gallery_existing_person_rejects_an_inconsistent_new_face() {
    let gallery = Gallery::new(None).unwrap();
    let person = gallery.enroll("One", &vector(0, None), None).unwrap();
    let id = person["id"].as_str().unwrap().to_string();
    assert!(gallery.enroll("One", &vector(1, None), Some(&id)).is_err());
    assert_eq!(gallery.people()[0]["samples"], 1);
    assert_eq!(gallery.generation(), 1);
}

#[test]
fn gallery_public_people_cannot_mutate_gallery() {
    let gallery = Gallery::new(None).unwrap();
    let person = gallery.enroll("One", &vector(0, None), None).unwrap();
    assert!(person.get("embeddings").is_none());
    assert_eq!(gallery.people().len(), 1);
    assert_eq!(gallery.match_person(&vector(0, None)).unwrap()["name"], "One");
}

#[test]
fn gallery_persists_only_embeddings_and_removal_survives_restart() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("gallery.json");
    let gallery = Gallery::new(Some(path.clone())).unwrap();
    let person = gallery.enroll("One", &vector(0, None), None).unwrap();
    let loaded = Gallery::new(Some(path.clone())).unwrap();
    assert_eq!(
        loaded.match_person(&vector(0, None)).unwrap()["person_id"],
        person["id"]
    );
    let data: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    assert!(!data.to_string().contains("photo"));
    assert_eq!(data["people"][0]["embeddings"][0].as_array().unwrap().len(), 128);
    loaded.remove(person["id"].as_str().unwrap()).unwrap();
    assert_eq!(Gallery::new(Some(path)).unwrap().people(), Vec::<Value>::new());
}

#[test]
fn gallery_invalid_persistence_fails_closed() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("gallery.json");
    for contents in [
        "not json",
        "{\"version\":99,\"people\":[]}",
        "{\"version\":1,\"people\":[{\"id\":\"one\",\"name\":\"One\",\"embeddings\":[[0]]}]}",
    ] {
        std::fs::write(&path, contents).unwrap();
        assert!(Gallery::new(Some(path.clone())).is_err(), "{contents}");
    }
}

#[test]
fn gallery_disk_write_failure_does_not_publish_a_partial_enrollment() {
    let dir = tempfile::tempdir().unwrap();
    // A file where the parent directory should be makes create_dir_all fail.
    let blocker = dir.path().join("blocker");
    std::fs::write(&blocker, b"file").unwrap();
    let gallery = Gallery::new(Some(blocker.join("gallery.json"))).unwrap();
    assert!(gallery.enroll("One", &vector(0, None), None).is_err());
    assert_eq!(gallery.people(), Vec::<Value>::new());
    assert_eq!(gallery.generation(), 0);
}

#[test]
fn gallery_concurrent_enrollments_keep_both_committed_people() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("gallery.json");
    let gallery = Arc::new(Gallery::new(Some(path.clone())).unwrap());
    let mut handles = Vec::new();
    for (name, index) in [("One", 0usize), ("Two", 1)] {
        let gallery = gallery.clone();
        handles.push(std::thread::spawn(move || {
            gallery.enroll(name, &vector(index, None), None)
        }));
    }
    for h in handles {
        h.join().unwrap().unwrap();
    }
    // Enrollment order is racy between the two threads; the invariant is that
    // both committed people survive (the Python test pins order with a blocked
    // persist hook, which this engine does not have).
    let mut names: Vec<_> = gallery
        .people()
        .iter()
        .map(|p| p["name"].as_str().unwrap().to_string())
        .collect();
    names.sort();
    assert_eq!(names, ["One", "Two"]);
    assert_eq!(gallery.generation(), 2);
    let mut names: Vec<_> = Gallery::new(Some(path))
        .unwrap()
        .people()
        .iter()
        .map(|p| p["name"].as_str().unwrap().to_string())
        .collect();
    names.sort();
    assert_eq!(names, ["One", "Two"]);
}

// ---------- engine (test_identity_engine.EngineTests) ----------

#[test]
fn engine_clear_face_has_normalized_coordinates_and_embedding() {
    let mut e = engine(vec![face(64.0, 48.0, 128.0, 96.0, 0.95)], vector(0, None));
    let result = e.infer(&jpeg(640, 480)).unwrap();
    assert_eq!(result.len(), 1);
    assert!((result[0].x - 0.1).abs() < 1e-6);
    assert!((result[0].y - 0.1).abs() < 1e-6);
    assert!((result[0].w - 0.2).abs() < 1e-6);
    assert!((result[0].h - 0.2).abs() < 1e-6);
    assert_eq!(result[0].embedding, vector(0, None));
}

#[test]
fn engine_enrollment_requires_one_raw_face_even_when_second_is_tiny() {
    let image = jpeg(640, 480);
    for rows in [
        vec![],
        vec![face(30.0, 30.0, 100.0, 100.0, 0.95), face(200.0, 30.0, 10.0, 10.0, 0.95)],
    ] {
        assert!(engine(rows, vector(0, None)).enroll_embedding(&image).is_err());
    }
    assert_eq!(
        engine(vec![face(30.0, 30.0, 100.0, 100.0, 0.95)], vector(0, None))
            .enroll_embedding(&image)
            .unwrap(),
        vector(0, None)
    );
}

#[test]
fn engine_live_rejects_small_invalid_or_nonfinite_faces() {
    let rows = vec![
        face(30.0, 30.0, 10.0, 100.0, 0.95),
        face(f32::NAN, 30.0, 100.0, 100.0, 0.95),
        face(30.0, 30.0, 100.0, 100.0, 0.2),
    ];
    assert!(engine(rows, vector(0, None)).infer(&jpeg(640, 480)).unwrap().is_empty());
    assert!(engine(vec![face(30.0, 30.0, 100.0, 100.0, 0.95)], vec![f32::NAN; 128])
        .enroll_embedding(&jpeg(640, 480))
        .is_err());
}

#[test]
fn engine_huge_or_nonimage_payload_is_rejected_before_decoder() {
    // 50000x50000 PNG header — rejected by header sniffing like the Python.
    let mut fake_png = b"\x89PNG\r\n\x1a\n".to_vec();
    fake_png.extend_from_slice(&13u32.to_be_bytes());
    fake_png.extend_from_slice(b"IHDR");
    fake_png.extend_from_slice(&50000u32.to_be_bytes());
    fake_png.extend_from_slice(&50000u32.to_be_bytes());
    fake_png.extend_from_slice(&[0x08, 0x02, 0, 0, 0]);
    fake_png.extend_from_slice(&[0u8; 4]);
    let mut e = engine(vec![], vector(0, None));
    for data in [fake_png, b"not an image".to_vec(), vec![0u8; 4 * 1024 * 1024 + 1]] {
        assert!(e.infer(&data).is_err(), "len {}", data.len());
    }
}

#[test]
fn engine_live_faces_have_bounded_recognition_work() {
    let rows: Vec<_> = (0..20).map(|i| face(20.0 + i as f32, 20.0, 100.0, 100.0, 0.95)).collect();
    // Wait — faces must be fully inside the frame; spread them within 640x480.
    let rows: Vec<_> = rows
        .into_iter()
        .enumerate()
        .map(|(i, mut r)| {
            r[0] = 20.0 + (i % 5) as f32 * 120.0;
            r[1] = 20.0 + (i / 5) as f32 * 110.0;
            for n in 0..5 {
                r[4 + 2 * n] = r[0] + 30.0 + 10.0 * (n % 2) as f32;
                r[4 + 2 * n + 1] = r[1] + 35.0 + 10.0 * (n / 2) as f32;
            }
            r
        })
        .collect();
    assert_eq!(engine(rows, vector(0, None)).infer(&jpeg(640, 480)).unwrap().len(), 8);
}

#[test]
fn engine_live_thumbnail_is_a_small_aligned_jpeg() {
    let mut e = engine(vec![face(30.0, 30.0, 100.0, 100.0, 0.95)], vector(0, None));
    let result = e.infer(&jpeg(640, 480)).unwrap();
    let thumb = result[0].thumbnail.as_ref().expect("thumbnail");
    let jpeg = base64::engine::general_purpose::STANDARD.decode(thumb).unwrap();
    assert!(jpeg.len() <= 8192);
    let crop = image::load_from_memory(&jpeg).unwrap();
    assert_eq!((crop.width(), crop.height()), (112, 112));
    assert_eq!(result[0].embedding, vector(0, None));
}

#[test]
fn engine_only_first_two_live_faces_carry_thumbnails() {
    let rows: Vec<_> = (0..8)
        .map(|i| face(20.0 + i as f32 * 70.0, 20.0, 60.0, 100.0, 0.95))
        .collect();
    let result = engine(rows, vector(0, None)).infer(&jpeg(640, 480)).unwrap();
    assert_eq!(result.len(), 8);
    assert!(result[..2].iter().all(|f| f.thumbnail.is_some()));
    assert!(result[2..].iter().all(|f| f.thumbnail.is_none()));
}

#[test]
fn engine_thumbnail_failure_or_oversize_does_not_break_recognition() {
    // Aligned crop that is not 112x112 — thumbnail is skipped like a failed
    // cv2.imencode.
    let mut e = engine_with_aligned(
        vec![face(30.0, 30.0, 100.0, 100.0, 0.95)],
        vector(0, None),
        Some(image::RgbImage::new(64, 64)),
    );
    let result = e.infer(&jpeg(640, 480)).unwrap();
    assert_eq!(result.len(), 1);
    assert!(result[0].thumbnail.is_none());
    // 112x112 high-frequency noise: a q65 JPEG cannot fit in 8 KiB.
    let mut noise = image::RgbImage::new(112, 112);
    for y in 0..112u32 {
        for x in 0..112u32 {
            let v = ((x.wrapping_mul(37) ^ y.wrapping_mul(101) ^ (x * y)) & 0xff) as u8;
            noise.put_pixel(x, y, image::Rgb([v, 255 - v, (x as u8) ^ v]));
        }
    }
    let mut e = engine_with_aligned(
        vec![face(30.0, 30.0, 100.0, 100.0, 0.95)],
        vector(0, None),
        Some(noise),
    );
    let result = e.infer(&jpeg(640, 480)).unwrap();
    assert_eq!(result.len(), 1);
    assert!(result[0].thumbnail.is_none());
}

#[test]
fn engine_enrollment_candidates_never_include_face_images() {
    let e = engine(vec![face(30.0, 30.0, 100.0, 100.0, 0.95)], vector(0, None));
    let candidate = e.faces(&jpeg(640, 480), true).unwrap();
    assert!(candidate[0].thumbnail.is_none());
    let gallery = Gallery::new(None).unwrap();
    assert_eq!(gallery.enroll("One", &candidate[0].embedding, None).unwrap()["samples"], 1);
}

#[test]
fn engine_png_is_supported_and_truncated_jpeg_rejected() {
    let mut e = engine(vec![face(30.0, 30.0, 100.0, 100.0, 0.95)], vector(0, None));
    assert_eq!(e.infer(&png(640, 480)).unwrap().len(), 1);
    assert!(e.infer(b"\xff\xd8\xff\xe0\x00\x20short").is_err());
}

#[test]
fn engine_portrait_detector_area_is_bounded_by_its_longest_side() {
    let mut e = engine(vec![face(5.0, 5.0, 50.0, 100.0, 0.95)], vector(0, None));
    let result = e.infer(&jpeg(480, 3840)).unwrap();
    assert_eq!(result.len(), 1);
    assert!((result[0].w - 0.625).abs() < 1e-5);
    assert!((result[0].h - 0.15625).abs() < 1e-5);
}

#[test]
fn engine_face_too_small_after_downsampling_is_not_named_or_enrolled() {
    let image = jpeg(3840, 2880);
    let mut e = engine(vec![face(30.0, 30.0, 20.0, 60.0, 0.95)], vector(0, None));
    assert!(e.infer(&image).unwrap().is_empty());
    assert!(e.enroll_embedding(&image).is_err());
}

#[test]
fn engine_resized_live_preserves_original_coordinate_scale() {
    let mut e = engine(vec![face(64.0, 48.0, 128.0, 96.0, 0.95)], vector(0, None));
    let result = e.infer(&jpeg(1280, 960)).unwrap();
    assert_eq!(result.len(), 1);
    assert!((result[0].x - 0.1).abs() < 1e-6);
    assert!((result[0].w - 0.2).abs() < 1e-6);
}

#[test]
fn image_dimensions_bounds() {
    assert!(image_dimensions(&jpeg(640, 480)).is_ok());
    assert!(image_dimensions(&png(640, 480)).is_ok());
    assert!(image_dimensions(b"").is_err());
    assert!(image_dimensions(b"not an image").is_err());
    assert!(image_dimensions(&jpeg(32, 32)).is_err()); // below the 64px floor
}

// ---------- service (test_identity_service.ServiceTests) ----------

fn test_camera() -> Arc<CameraService> {
    Arc::new(CameraService::new(Arc::new(MapState::new(false)), None, false))
}

fn set_frame(camera: &CameraService, jpeg: &[u8], received: f64, id: i64, source: &str) {
    camera.set_phone_rgb(Some(CameraFrame {
        jpeg: jpeg.to_vec(),
        received,
        frame_id: id,
        source: source.into(),
        capture_verified: false,
        capture_binding: None,
        tracking: None,
        mirrored: false,
        socket_received: None,
        published: None,
    }));
}

#[derive(Default)]
struct FakeEngine {
    /// Runs inside infer; used to block or to mutate the gallery mid-flight.
    infer_hook: Option<Box<dyn FnMut() + Send>>,
    enroll_hook: Option<Box<dyn FnMut() -> Vec<f32> + Send>>,
    faces: Vec<DetectedFace>,
}

impl FakeEngine {
    fn one_face() -> Self {
        Self {
            infer_hook: None,
            enroll_hook: None,
            faces: vec![DetectedFace {
                x: 0.1, y: 0.2, w: 0.3, h: 0.4, conf: 0.95,
                embedding: feature(0),
                thumbnail: None,
            }],
        }
    }
}

impl IdentityInfer for FakeEngine {
    fn infer(&mut self, _jpeg: &[u8]) -> Result<Vec<DetectedFace>, String> {
        if let Some(h) = &mut self.infer_hook {
            h();
        }
        Ok(self.faces.clone())
    }
    fn enroll_embedding(&mut self, _image: &[u8]) -> Result<Vec<f32>, String> {
        if let Some(h) = &mut self.enroll_hook {
            return Ok(h());
        }
        Ok(feature(0))
    }
}

fn service_with_engine(camera: &Arc<CameraService>) -> Arc<IdentityService> {
    let s = Arc::new(IdentityService::new(camera.clone(), None, None, None, 3.0));
    *s.engine.lock().unwrap() = Some(Box::new(FakeEngine::one_face()));
    s
}

#[test]
fn service_unknown_faces_and_original_frame_age() {
    let camera = test_camera();
    set_frame(&camera, b"jpeg", now_monotonic(), 1, "PHONE RGB");
    let s = service_with_engine(&camera);
    s.gallery.read().unwrap().enroll("Mehdi", &feature(0), None).unwrap();
    // Unknown path first.
    let person_id = {
        let people = s.gallery.read().unwrap().people();
        people[0]["id"].as_str().unwrap().to_string()
    };
    s.gallery.read().unwrap().remove(&person_id).unwrap();
    s.process_once();
    let packet = s.snapshot();
    assert_eq!(packet["faces"][0]["name"], "Unknown");
    assert_eq!(packet["frame"], 1);
    assert_eq!(packet["received"].as_f64().unwrap(), s.camera.snapshot().unwrap().received);
    // A stale camera frame makes the packet unfresh and empties faces.
    set_frame(&camera, b"jpeg", now_monotonic() - 2.0, 1, "PHONE RGB");
    assert_eq!(s.snapshot()["fresh"], false);
    assert_eq!(s.snapshot()["faces"], json!([]));
}

#[test]
fn service_replaced_source_and_deleted_gallery_invalidate_names() {
    let camera = test_camera();
    set_frame(&camera, b"jpeg", now_monotonic(), 1, "PHONE RGB");
    let s = service_with_engine(&camera);
    let person = s.gallery.read().unwrap().enroll("Mehdi", &feature(0), None).unwrap();
    s.process_once();
    assert_eq!(s.snapshot()["faces"][0]["name"], "Mehdi");
    s.remove(person["id"].as_str().unwrap()).unwrap();
    assert_eq!(s.snapshot()["faces"], json!([]));
    s.gallery.read().unwrap().enroll("Mehdi", &feature(0), None).unwrap();
    set_frame(&camera, b"other", now_monotonic(), 1, "USB CAMERA");
    assert_eq!(s.snapshot()["faces"], json!([]));
}

#[test]
fn service_slow_inference_does_not_publish_deleted_name() {
    let camera = test_camera();
    set_frame(&camera, b"jpeg", now_monotonic(), 1, "PHONE RGB");
    let s = service_with_engine(&camera);
    let person = s.gallery.read().unwrap().enroll("Mehdi", &feature(0), None).unwrap();
    let entered = Arc::new(AtomicBool::new(false));
    let release = Arc::new(AtomicBool::new(false));
    {
        let entered = entered.clone();
        let release = release.clone();
        *s.engine.lock().unwrap() = Some(Box::new(FakeEngine {
            infer_hook: Some(Box::new(move || {
                entered.store(true, Ordering::SeqCst);
                while !release.load(Ordering::SeqCst) {
                    std::thread::sleep(Duration::from_millis(5));
                }
            })),
            enroll_hook: None,
            faces: FakeEngine::one_face().faces,
        }));
    }
    let worker = {
        let s = s.clone();
        std::thread::spawn(move || s.process_once())
    };
    for _ in 0..400 {
        if entered.load(Ordering::SeqCst) {
            break;
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    s.remove(person["id"].as_str().unwrap()).unwrap();
    release.store(true, Ordering::SeqCst);
    worker.join().unwrap();
    assert_eq!(s.snapshot()["faces"], json!([]));
}

#[test]
fn service_deletion_after_generation_check_cannot_retag_old_name() {
    let camera = test_camera();
    set_frame(&camera, b"jpeg", now_monotonic(), 1, "PHONE RGB");
    let s = service_with_engine(&camera);
    let person = s.gallery.read().unwrap().enroll("One", &feature(0), None).unwrap();
    let gallery = s.gallery.clone();
    let id = person["id"].as_str().unwrap().to_string();
    *s.engine.lock().unwrap() = Some(Box::new(FakeEngine {
        infer_hook: Some(Box::new(move || {
            gallery.read().unwrap().remove(&id).unwrap();
        })),
        enroll_hook: None,
        faces: FakeEngine::one_face().faces,
    }));
    s.process_once();
    assert_eq!(s.gallery.read().unwrap().people(), Vec::<Value>::new());
    assert_eq!(s.snapshot()["faces"], json!([]));
}

#[tokio::test]
async fn service_busy_enrollment_rejected_without_queue() {
    let camera = test_camera();
    let s = service_with_engine(&camera);
    let _guard = s.gate.clone().lock_owned().await;
    let err = s.enroll("Mehdi".into(), b"image".to_vec(), None).await.unwrap_err();
    assert!(err.contains("busy"), "{err}");
}

#[tokio::test]
async fn service_one_enrollment_waits_for_next_slot_without_extra_queue() {
    let camera = test_camera();
    let s = service_with_engine(&camera);
    let guard = s.gate.clone().lock_owned().await;
    let first = {
        let s = s.clone();
        tokio::spawn(async move { s.enroll("One".into(), b"image".to_vec(), None).await })
    };
    tokio::time::sleep(Duration::from_millis(20)).await;
    let err = s.enroll("Two".into(), b"image".to_vec(), None).await.unwrap_err();
    assert!(err.contains("busy"), "{err}");
    drop(guard);
    let result = first.await.unwrap().unwrap();
    assert_eq!(result["name"], "One");
}

#[test]
fn service_old_inference_age_is_never_renewed() {
    let camera = test_camera();
    set_frame(&camera, b"jpeg", now_monotonic(), 1, "PHONE RGB");
    let s = service_with_engine(&camera);
    s.gallery.read().unwrap().enroll("Mehdi", &feature(0), None).unwrap();
    *s.engine.lock().unwrap() = Some(Box::new(FakeEngine {
        infer_hook: Some(Box::new(|| std::thread::sleep(Duration::from_millis(1100)))),
        enroll_hook: None,
        faces: FakeEngine::one_face().faces,
    }));
    s.process_once();
    let packet = s.snapshot();
    assert!(packet["age_ms"].as_i64().unwrap() >= 1000);
    assert_eq!(packet["fresh"], false);
    assert_eq!(packet["faces"], json!([]));
}

#[test]
fn service_capture_binding_replaced_and_frame_counter_reset() {
    let camera = test_camera();
    set_frame(&camera, b"jpeg", now_monotonic(), 1, "PHONE RGB");
    let s = service_with_engine(&camera);
    s.gallery.read().unwrap().enroll("Mehdi", &feature(0), None).unwrap();
    s.process_once();
    assert_eq!(s.snapshot()["faces"].as_array().unwrap().len(), 1);
    // A rewound frame counter cannot re-tag the old observation.
    set_frame(&camera, b"jpeg", now_monotonic(), 0, "PHONE RGB");
    assert_eq!(s.snapshot()["faces"], json!([]));
    // Nor can a different capture-binding identity.
    camera.set_phone_rgb(Some(CameraFrame {
        jpeg: b"jpeg".to_vec(),
        received: now_monotonic(),
        frame_id: 2,
        source: "PHONE RGB".into(),
        capture_verified: true,
        tracking: None,
        mirrored: false,
        socket_received: None,
        published: None,
        capture_binding: Some(Arc::new(
            farsight_server::capture_permits::CapturePermits::new("s", 0, false),
        )),
        tracking: None,
        mirrored: false,
        socket_received: None,
        published: None,
    }));
    assert_eq!(s.snapshot()["faces"], json!([]));
}

#[tokio::test]
async fn service_enrollment_cancellation_keeps_worker_gate_until_done() {
    let camera = test_camera();
    let s = service_with_engine(&camera);
    let entered = Arc::new(AtomicBool::new(false));
    let release = Arc::new(AtomicBool::new(false));
    {
        let entered = entered.clone();
        let release = release.clone();
        *s.engine.lock().unwrap() = Some(Box::new(FakeEngine {
            infer_hook: None,
            enroll_hook: Some(Box::new(move || {
                entered.store(true, Ordering::SeqCst);
                while !release.load(Ordering::SeqCst) {
                    std::thread::sleep(Duration::from_millis(5));
                }
                feature(0)
            })),
            faces: FakeEngine::one_face().faces,
        }));
    }
    let task = {
        let s = s.clone();
        tokio::spawn(async move { s.enroll("One".into(), b"image".to_vec(), None).await })
    };
    for _ in 0..400 {
        if entered.load(Ordering::SeqCst) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    task.abort();
    // The cancelled upload still holds the worker gate: a second upload must
    // see 'busy', and the gate itself stays locked until inference finishes.
    assert!(s.gate.try_lock().is_err());
    let err = s.enroll("Two".into(), b"image".to_vec(), None).await.unwrap_err();
    assert!(err.contains("busy"), "{err}");
    release.store(true, Ordering::SeqCst);
    for _ in 0..400 {
        if s.gate.try_lock().is_ok() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    for _ in 0..400 {
        if s.people().len() == 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert_eq!(s.people().len(), 1);
}

#[tokio::test]
async fn service_optional_model_startup_and_failure_are_nonfatal() {
    let camera = test_camera();
    let service = IdentityService::new(camera.clone(), None, None, None, 3.0);
    let service = Arc::new(service);
    service.clone().start();
    assert_eq!(service.status()["enabled"], false);
    assert_eq!(service.status()["ready"], false);
    service.close();
    let service = Arc::new(IdentityService::new(
        camera,
        Some("/missing/yunet".into()),
        Some("/missing/sface".into()),
        None,
        3.0,
    ));
    service.clone().start();
    assert_eq!(service.status()["ready"], false);
    assert!(service.status()["error"].as_str().map(|e| !e.is_empty()).unwrap_or(false));
    service.close();
}

// ---------- API (test_identity_service.ApiTests + server tests) ----------

fn api_server(service: Arc<IdentityService>) -> (tokio::task::JoinHandle<()>, String) {
    let router: axum::Router = farsight_server::identity::router(service);
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let addr = listener.local_addr().unwrap();
    listener.set_nonblocking(true).unwrap();
    let listener = tokio::net::TcpListener::from_std(listener).unwrap();
    let handle = tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });
    (handle, format!("http://{addr}"))
}

#[tokio::test]
async fn api_enroll_list_remove_and_no_embeddings_in_response() {
    let camera = test_camera();
    let service = service_with_engine(&camera);
    let (_server, base) = api_server(service.clone());
    let client = reqwest::Client::new();
    let result = client
        .post(format!("{base}/phone/identity/people"))
        .json(&json!({
            "name": "Mehdi", "consent": true,
            "image": base64::engine::general_purpose::STANDARD.encode(b"image")
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(result.status().as_u16(), 200);
    let record: Value = result.json().await.unwrap();
    assert_eq!(record["name"], "Mehdi");
    assert!(record.get("embedding").is_none());
    assert_eq!(service.people().len(), 1);
    let listed: Value = client
        .get(format!("{base}/phone/identity/people"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(listed["people"].as_array().unwrap().len(), 1);
    let removed = client
        .delete(format!("{base}/phone/identity/people/{}", record["id"].as_str().unwrap()))
        .send()
        .await
        .unwrap()
        .status()
        .as_u16();
    assert_eq!(removed, 200);
    let after: Value = client
        .get(format!("{base}/phone/identity/people"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(after["people"], json!([]));
}

#[tokio::test]
async fn api_consent_and_bad_base64_and_body_limit() {
    let camera = test_camera();
    let service = service_with_engine(&camera);
    let (_server, base) = api_server(service);
    let client = reqwest::Client::new();
    for payload in [
        json!({"name": "Mehdi", "image": "aW1hZ2U="}),
        json!({"name": "Mehdi", "consent": "true", "image": "aW1hZ2U="}),
        json!({"name": "Mehdi", "consent": true, "image": "not base64!"}),
    ] {
        let status = client
            .post(format!("{base}/phone/identity/people"))
            .json(&payload)
            .send()
            .await
            .unwrap()
            .status()
            .as_u16();
        assert_eq!(status, 422);
    }
    let too_large = client
        .post(format!("{base}/phone/identity/people"))
        .body(vec![b'x'; 6 * 1024 * 1024 + 1])
        .send()
        .await
        .unwrap()
        .status()
        .as_u16();
    assert_eq!(too_large, 413);
}

#[tokio::test]
async fn api_page_and_disabled_status() {
    let camera = test_camera();
    // No engine: the disabled service still answers every route.
    let service = Arc::new(IdentityService::new(camera, None, None, None, 3.0));
    let (_server, base) = api_server(service);
    let client = reqwest::Client::new();
    let page = client
        .get(format!("{base}/phone/identity/enroll"))
        .send()
        .await
        .unwrap();
    assert_eq!(page.status().as_u16(), 200);
    assert_eq!(
        page.headers().get("cache-control").and_then(|v| v.to_str().ok()),
        Some("no-store")
    );
    let body = page.text().await.unwrap();
    assert!(body.contains("identity") || body.contains("enroll"));
    let status: Value = client
        .get(format!("{base}/phone/identity/status"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(status["ready"], false);
    let enroll_status = client
        .post(format!("{base}/phone/identity/people"))
        .json(&json!({"name": "Mehdi", "consent": true, "image": "aW1hZ2U="}))
        .send()
        .await
        .unwrap()
        .status()
        .as_u16();
    assert_eq!(enroll_status, 503);
}

#[tokio::test]
async fn api_websocket_and_missing_person() {
    use futures_util::StreamExt;
    let camera = test_camera();
    let service = service_with_engine(&camera);
    let (_server, base) = api_server(service);
    let client = reqwest::Client::new();
    assert_eq!(
        client
            .delete(format!("{base}/phone/identity/people/missing"))
            .send()
            .await
            .unwrap()
            .status()
            .as_u16(),
        404
    );
    let ws_base = base.replacen("http://", "ws://", 1);
    let (mut ws, _) = tokio_tungstenite::connect_async(format!("{ws_base}/ws/identity"))
        .await
        .unwrap();
    let message = tokio::time::timeout(Duration::from_secs(5), ws.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let packet: Value = serde_json::from_str(&message.into_text().unwrap()).unwrap();
    assert_eq!(packet["type"], "identity");
    assert_eq!(packet["fresh"], false);
    assert_eq!(packet["faces"], json!([]));
    assert!(packet.get("gallery_generation").is_some());
    assert!(packet.get("age_ms").is_some());
    assert!(packet.get("error").is_some());
}

#[tokio::test]
async fn api_gallery_delete_failure_is_visible_and_retains_enrollment() {
    let camera = test_camera();
    let service = service_with_engine(&camera);
    // Seed a persistent gallery, then remove the parent directory so the
    // atomic-rename persist fails — the person must stay enrolled.
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("gallery.json");
    let gallery = Gallery::new(Some(path)).unwrap();
    let person = gallery.enroll("Mehdi", &feature(0), None).unwrap();
    std::fs::remove_dir_all(dir.path()).unwrap();
    std::fs::write(dir.path(), b"not a dir").unwrap();
    *service.gallery.write().unwrap() = gallery;
    let (_server, base) = api_server(service.clone());
    let client = reqwest::Client::new();
    let status = client
        .delete(format!(
            "{base}/phone/identity/people/{}",
            person["id"].as_str().unwrap()
        ))
        .send()
        .await
        .unwrap()
        .status()
        .as_u16();
    assert_eq!(status, 503);
    assert_eq!(service.people().len(), 1);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn api_server_level_disabled_and_broken_models_leave_routes_up() {
    // test_identity_server parity: a full app build serves identity routes
    // disabled, and broken model paths only flip status, never the server.
    let state = farsight_server::server::build_state(
        false, None, None, false, "off", None, None, false, None, 100, 0,
        None, None, None, None, None, None, None, None, None, 5.0, None,
        false, 120.0, None, 90.0, 15, 500_000, false, None, None, false,
        false, "servo", None, false, false, 0.45, 3.0, 0.7, 0.8, 0.15,
        None, false, (0, 0), false, false, (None, None, None),
    );
    let router = farsight_server::server::router(state.clone(), None);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let base = format!("http://{addr}");
    let client = reqwest::Client::new();
    let status: Value = client
        .get(format!("{base}/phone/identity/status"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(status["enabled"], false);
    assert_eq!(status["ready"], false);
    let page = client
        .get(format!("{base}/phone/identity/enroll"))
        .send()
        .await
        .unwrap()
        .status()
        .as_u16();
    assert_eq!(page, 200);

    // Broken model paths: enabled + error, ready false, other routes alive.
    let state = farsight_server::server::build_state(
        false, None, None, false, "off", None, None, false, None, 100, 0,
        None, None, None, None, None, None, None, None, None, 5.0, None,
        false, 120.0, None, 90.0, 15, 500_000, false, None, None, false,
        false, "servo", None, false, false, 0.45, 3.0, 0.7, 0.8, 0.15,
        None, false, (0, 0), false, false,
        (Some("/missing/yunet.onnx".into()), Some("/missing/sface.onnx".into()), None),
    );
    state.identity.clone().start();
    let status = state.identity.status();
    assert_eq!(status["enabled"], true);
    assert_eq!(status["ready"], false);
    assert!(status["error"].as_str().map(|e| !e.is_empty()).unwrap_or(false));
    let router = farsight_server::server::router(state, None);
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move { axum::serve(listener, router).await.unwrap() });
    let base = format!("http://{addr}");
    let control = client
        .get(format!("{base}/control/status"))
        .send()
        .await
        .unwrap()
        .status()
        .as_u16();
    assert_eq!(control, 200);
    let status: Value = client
        .get(format!("{base}/phone/identity/status"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(status["enabled"], true);
    assert_eq!(status["ready"], false);
}
