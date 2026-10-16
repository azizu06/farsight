//! macOS engines for farsight-views, all behind the `vision` feature:
//!
//! * `person_mask` — VNGeneratePersonSegmentationRequest (qualityLevel
//!   = VNQualityBalanced, which Apple schedules on the Neural Engine) via
//!   objc2 runtime calls. Produces the same transient-mask byte layout as
//!   the temporal-median path so the trainer side never changes.
//! * `sharpness` — Laplacian-variance blur score via Accelerate's
//!   vImageConvolve_Planar8 instead of the Rust scalar path.
//!
//! None of this links on Linux; callers are `#[cfg]`-selected.
#![cfg(all(target_os = "macos", feature = "vision"))]

use anyhow::{bail, Context, Result};
use objc2::runtime::{AnyClass, AnyObject};
use objc2::msg_send;
use std::ffi::c_void;
use std::path::Path;

#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    fn CFRelease(obj: *const c_void);
}

#[link(name = "ImageIO", kind = "framework")]
extern "C" {
    fn CGImageSourceCreateWithURL(url: *const c_void, options: *const c_void)
        -> *const c_void;
    fn CGImageSourceCreateImageAtIndex(
        src: *const c_void,
        index: usize,
        options: *const c_void,
    ) -> *const c_void;
}

#[link(name = "CoreVideo", kind = "framework")]
extern "C" {
    fn CVPixelBufferLockBaseAddress(p: *const c_void, flags: u64) -> i32;
    fn CVPixelBufferUnlockBaseAddress(p: *const c_void, flags: u64) -> i32;
    fn CVPixelBufferGetBaseAddress(p: *const c_void) -> *const c_void;
    fn CVPixelBufferGetWidth(p: *const c_void) -> usize;
    fn CVPixelBufferGetHeight(p: *const c_void) -> usize;
    fn CVPixelBufferGetBytesPerRow(p: *const c_void) -> usize;
}

#[repr(C)]
struct VImageBuffer {
    data: *mut c_void,
    height: usize,
    width: usize,
    row_bytes: usize,
}

#[link(name = "Accelerate", kind = "framework")]
extern "C" {
    fn vImageConvolve_Planar8(
        src: *const VImageBuffer,
        dst: *const VImageBuffer,
        temp: *mut c_void,
        off_x: usize,
        off_y: usize,
        kernel: *const i16,
        kh: u32,
        kw: u32,
        divisor: i32,
        bg: u8,
        flags: u32,
    ) -> i64;
}

/// File path → retained CGImage via NSString/NSURL toll-free bridging.
unsafe fn load_cgimage(path: &Path) -> Result<*const c_void> {
    let s = path.to_str().context("non-utf8 path")?;
    let cls = AnyClass::get(c"NSString").context("NSString")?;
    let ns: *mut AnyObject = msg_send![cls, stringWithUTF8String: s.as_ptr()];
    if ns.is_null() {
        bail!("NSString alloc failed");
    }
    let url_cls = AnyClass::get(c"NSURL").context("NSURL")?;
    let url: *mut AnyObject = msg_send![url_cls, fileURLWithPath: ns];
    let _: *mut AnyObject = msg_send![ns, release];
    if url.is_null() {
        bail!("NSURL alloc failed");
    }
    let src = CGImageSourceCreateWithURL(url.cast(), std::ptr::null());
    let _: *mut AnyObject = msg_send![url, release];
    if src.is_null() {
        bail!("CGImageSourceCreateWithURL failed for {}", path.display());
    }
    let img = CGImageSourceCreateImageAtIndex(src, 0, std::ptr::null());
    CFRelease(src);
    if img.is_null() {
        bail!("CGImageSourceCreateImageAtIndex failed");
    }
    Ok(img)
}

/// Person-segmentation confidence mask for one image file.
/// Returns (w, h, bytes) where byte > 128 means "person" (transient).
/// Runs VNGeneratePersonSegmentationRequest at VNQualityBalanced — Apple's
/// balanced tier targets the Neural Engine. Callers may run this on a
/// worker thread; Vision serialises ANE access internally.
pub unsafe fn person_mask(path: &Path) -> Result<(usize, usize, Vec<u8>)> {
    objc2::rc::autoreleasepool(|_pool| {
        let img = load_cgimage(path)?;
        // VNGeneratePersonSegmentationRequest
        let req_cls = AnyClass::get(c"VNGeneratePersonSegmentationRequest")
            .context("VNGeneratePersonSegmentationRequest")?;
        let req: *mut AnyObject = msg_send![req_cls, alloc];
        let req: *mut AnyObject = msg_send![req, init];
        if req.is_null() {
            bail!("VNGeneratePersonSegmentationRequest init failed");
        }
        // qualityLevel: VNQualityBalanced = 1 (ANE-friendly).
        let _: () = msg_send![req, setQualityLevel: 1usize];
        let _: () = msg_send![req, setOutputPixelFormat: 0x4C303038u32]; // 'L008' one-component 8-bit
        // NSArray of one request.
        let arr_cls = AnyClass::get(c"NSArray").context("NSArray")?;
        let arr: *mut AnyObject = msg_send![arr_cls, arrayWithObject: req];
        // Handler bound to the CGImage.
        let h_cls = AnyClass::get(c"VNImageRequestHandler").context("VNImageRequestHandler")?;
        let h: *mut AnyObject = msg_send![h_cls, alloc];
        let h: *mut AnyObject = msg_send![h, initWithCGImage: img, options: std::ptr::null::<c_void>()];
        if h.is_null() {
            bail!("VNImageRequestHandler init failed");
        }
        let mut err: *mut AnyObject = std::ptr::null_mut();
        let ok: bool = msg_send![h, performRequests: arr, error: &mut err];
        if !ok {
            CFRelease(img);
            bail!("Vision performRequests failed");
        }
        // results → VNPixelBufferObservation → pixelBuffer
        let res: *mut AnyObject = msg_send![req, results];
        let obs: *mut AnyObject = msg_send![res, firstObject];
        if obs.is_null() {
            CFRelease(img);
            bail!("person segmentation returned no observation");
        }
        let pb: *mut c_void = msg_send![obs, pixelBuffer];
        if pb.is_null() {
            CFRelease(img);
            bail!("observation has no pixelBuffer");
        }
        CVPixelBufferLockBaseAddress(pb.cast(), 1); // read-only
        let (w, hgt) = (
            CVPixelBufferGetWidth(pb.cast()),
            CVPixelBufferGetHeight(pb.cast()),
        );
        let stride = CVPixelBufferGetBytesPerRow(pb.cast());
        let base = CVPixelBufferGetBaseAddress(pb.cast()) as *const u8;
        let mut out = Vec::with_capacity(w * hgt);
        for y in 0..hgt {
            let row = base.add(y * stride);
            out.extend_from_slice(std::slice::from_raw_parts(row, w));
        }
        CVPixelBufferUnlockBaseAddress(pb.cast(), 1);
        let _: () = msg_send![h, release];
        let _: () = msg_send![req, release];
        CFRelease(img);
        Ok((w, hgt, out))
    })
}

/// Laplacian-variance blur score via vImageConvolve_Planar8 on a
/// pre-decoded 8-bit luma plane. Same metric as the Rust path.
pub fn sharpness_vimage(w: usize, h: usize, luma_plane: &[u8]) -> f64 {
    let mut src = luma_plane.to_vec();
    let mut dst = vec![0u8; w * h];
    let kernel: [i16; 9] = [0, 1, 0, 1, -4, 1, 0, 1, 0];
    let sb = VImageBuffer {
        data: src.as_mut_ptr().cast(),
        height: h,
        width: w,
        row_bytes: w,
    };
    let mut db = VImageBuffer {
        data: dst.as_mut_ptr().cast(),
        height: h,
        width: w,
        row_bytes: w,
    };
    let rc = unsafe {
        vImageConvolve_Planar8(
            &sb, &mut db, std::ptr::null_mut(), 0, 0,
            kernel.as_ptr(), 3, 3, 1, 0, 0,
        )
    };
    if rc != 0 {
        return 0.0;
    }
    // variance of the response, skipping the 1-px border
    let (mut m, mut m2, mut n) = (0f64, 0f64, 0f64);
    for y in 1..h - 1 {
        for x in 1..w - 1 {
            let v = dst[y * w + x] as f64 - 128.0;
            m += v;
            m2 += v * v;
            n += 1.0;
        }
    }
    if n == 0.0 {
        return 0.0;
    }
    m2 / n - (m / n) * (m / n)
}
