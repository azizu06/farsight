use ball_tree::BallTree;
use brush_render::{
    bounding_box::BoundingBox,
    camera::Camera,
    gaussian_splats::{SplatRenderMode, Splats, inverse_sigmoid},
};
use brush_serde::SplatData;
use burn::{config::Config, tensor::Device};
use glam::Vec3;
use rand::{Rng, RngExt};
use rayon::iter::{IntoParallelRefIterator, ParallelIterator};
use tracing::trace_span;

#[derive(Config, Debug)]
pub struct RandomSplatsConfig {
    #[config(default = 10000)]
    pub init_count: usize,
}

/// Estimate scene scale from camera positions.
///
/// Uses the average nearest-neighbor distance between cameras,
/// with a minimum of 1.0 (1 meter baseline).
fn estimate_scene_scale(cameras: &[Camera]) -> f32 {
    if cameras.len() < 2 {
        return 1.0;
    }

    let mut total_nn_dist = 0.0f32;
    for (i, cam) in cameras.iter().enumerate() {
        let mut min_dist = f32::INFINITY;
        for (j, other) in cameras.iter().enumerate() {
            if i != j {
                let d = cam.position.distance(other.position);
                if d < min_dist {
                    min_dist = d;
                }
            }
        }
        total_nn_dist += min_dist;
    }

    let avg_nn = total_nn_dist / cameras.len() as f32;
    // Scene depth is roughly a few multiples of the camera spacing.
    // Use 3x the average spacing, with 1m floor.
    (avg_nn * 3.0).max(1.0)
}

/// Create initial splats by sampling random points inside camera frustums.
///
/// For each splat, a random camera is chosen, then a random ray direction
/// within its field of view is sampled, and a random depth along that ray
/// is picked.
pub fn create_random_splats(
    config: &RandomSplatsConfig,
    cameras: &[Camera],
    scene_scale_override: Option<f32>,
    rng: &mut impl Rng,
    mode: SplatRenderMode,
    device: &Device,
) -> Splats {
    let num_points = config.init_count;
    let scene_scale = scene_scale_override.unwrap_or_else(|| estimate_scene_scale(cameras));

    let near = scene_scale * 0.05;
    let far = scene_scale;
    let ln_near = near.ln();
    let ln_far = far.ln();

    // Sample points in camera frustums
    let positions: Vec<f32> = (0..num_points)
        .flat_map(|_| {
            let cam = &cameras[rng.random_range(0..cameras.len())];
            let local_to_world = cam.local_to_world();

            // Random direction within the camera's FOV
            let half_fov_x = (cam.fov_x * 0.5) as f32;
            let half_fov_y = (cam.fov_y * 0.5) as f32;
            let dx = rng.random_range(-half_fov_x..half_fov_x).tan();
            let dy = rng.random_range(-half_fov_y..half_fov_y).tan();

            // Log-uniform depth so we don't over-pack near the camera
            let depth = (rng.random_range(ln_near..ln_far)).exp();

            // Camera looks along -Z in local space
            let local_point = Vec3::new(dx * depth, dy * depth, -depth);
            let world_point = local_to_world.transform_point3(local_point);

            [world_point.x, world_point.y, world_point.z]
        })
        .collect();

    // Random colors
    let sh_coeffs: Vec<f32> = (0..num_points)
        .flat_map(|_| {
            [
                rng.random_range(0.0..1.0),
                rng.random_range(0.0..1.0),
                rng.random_range(0.0..1.0),
            ]
        })
        .collect();

    // Random rotations (normalized quaternions)
    let rotations: Vec<f32> = (0..num_points)
        .flat_map(|_| {
            let x: f32 = rng.random_range(-1.0..1.0);
            let y: f32 = rng.random_range(-1.0..1.0);
            let z: f32 = rng.random_range(-1.0..1.0);
            let w: f32 = rng.random_range(-1.0..1.0);
            let len = (x * x + y * y + z * z + w * w).sqrt().max(1e-6);
            [x / len, y / len, z / len, w / len]
        })
        .collect();

    // Random opacities
    let opacities: Vec<f32> = (0..num_points)
        .map(|_| rng.random_range(inverse_sigmoid(0.1)..inverse_sigmoid(0.25)))
        .collect();

    // Scale based on scene scale and point density
    let default_scale = (scene_scale / (num_points as f32).cbrt()).ln();
    let log_scales: Vec<f32> = vec![default_scale; num_points * 3];

    Splats::from_raw(
        positions, rotations, log_scales, sh_coeffs, opacities, mode, device,
    )
}

pub fn bounds_from_pos(percentile: f32, means: &[f32]) -> BoundingBox {
    let (mut x_vals, mut y_vals, mut z_vals): (Vec<f32>, Vec<f32>, Vec<f32>) = means
        .chunks_exact(3)
        .map(|chunk| (chunk[0], chunk[1], chunk[2]))
        .collect();
    x_vals.retain(|x| x.is_finite());
    y_vals.retain(|y| y.is_finite());
    z_vals.retain(|z| z.is_finite());

    // If any axis is entirely non-finite, fall back to a unit box rather
    // than panicking on the percentile index.
    if x_vals.is_empty() || y_vals.is_empty() || z_vals.is_empty() {
        return BoundingBox::from_min_max(Vec3::splat(-1.0), Vec3::splat(1.0));
    }

    x_vals.sort_by(|a, b| a.total_cmp(b));
    y_vals.sort_by(|a, b| a.total_cmp(b));
    z_vals.sort_by(|a, b| a.total_cmp(b));

    let pick = |vals: &[f32]| -> (f32, f32) {
        let n = vals.len();
        let lo = ((1.0 - percentile) / 2.0 * n as f32) as usize;
        let hi = (n - 1).min(((1.0 + percentile) / 2.0 * n as f32) as usize);
        (vals[lo], vals[hi])
    };

    let (xmin, xmax) = pick(&x_vals);
    let (ymin, ymax) = pick(&y_vals);
    let (zmin, zmax) = pick(&z_vals);
    BoundingBox::from_min_max(Vec3::new(xmin, ymin, zmin), Vec3::new(xmax, ymax, zmax))
}

#[derive(PartialEq, Clone, Copy, Debug)]
struct BallPoint(glam::Vec3A);

impl ball_tree::Point for BallPoint {
    fn distance(&self, other: &Self) -> f64 {
        self.0.distance(other.0) as f64
    }

    fn move_towards(&self, other: &Self, d: f64) -> Self {
        Self(self.0.lerp(other.0, d as f32 / self.0.distance(other.0)))
    }

    fn midpoint(a: &Self, b: &Self) -> Self {
        Self((a.0 + b.0) / 2.0)
    }
}

/// Compute scales using KNN based on point density.
fn compute_knn_scales(pos_data: &[f32]) -> Vec<f32> {
    let _ = trace_span!("compute_knn_scales").entered();

    let n_splats = pos_data.len() / 3;

    if n_splats < 3 {
        return vec![0.0; n_splats * 3];
    }

    let bounding_box = trace_span!("Bounds from pose").in_scope(|| bounds_from_pos(0.75, pos_data));
    let median_size = bounding_box.median_size().max(0.01);

    trace_span!("Splats KNN scale init").in_scope(|| {
        let tree_points: Vec<BallPoint> = pos_data
            .as_chunks::<3>()
            .0
            .iter()
            .map(|v| BallPoint(glam::Vec3A::new(v[0], v[1], v[2])))
            .collect();

        let empty = vec![(); tree_points.len()];
        let tree = BallTree::new(tree_points.clone(), empty);

        tree_points
            .par_iter()
            .map_with(tree.query(), |query, p| {
                // Get half of the average of 2 nearest distances.
                let mut q = query.nn(p).skip(1);
                let a1 = q.next().unwrap().1 as f32;
                let a2 = q.next().unwrap().1 as f32;
                let dist = (a1 + a2) / 4.0;
                dist.clamp(1e-3, median_size * 0.1).ln()
            })
            .flat_map(|p| [p, p, p])
            .collect()
    })
}

/// Importance-decimate a loaded point cloud to ~`target` points.
///
/// Coverage-preserving alternative to [`SplatData::subsample`]'s strided
/// take for dense measured seeds (`LiDAR` dots): pick the voxel size whose
/// occupancy is closest to `target` (binary search), then keep one
/// representative point per occupied voxel — the point closest to the voxel
/// centre. Uniform coverage beats strided subsampling whenever the file
/// order clusters points (scan-line ordering), and sparse regions keep
/// their few points rather than losing them to a stride.
///
/// O(n) hashing — no kNN tree — so a 1.45M-point seed decimates in well
/// under a second. Runs on the host before any GPU upload.
pub fn decimate_splat_data(data: SplatData, target: usize) -> SplatData {
    let n = data.num_splats();
    if target == 0 || n <= target {
        return data;
    }
    let pos = &data.means;

    // Scene bounds for the voxel-size search range.
    let mut mn = [f32::MAX; 3];
    let mut mx = [f32::MIN; 3];
    for p in pos.chunks_exact(3) {
        for a in 0..3 {
            if p[a].is_finite() {
                mn[a] = mn[a].min(p[a]);
                mx[a] = mx[a].max(p[a]);
            }
        }
    }
    let extent = (mx[0] - mn[0]).max(mx[1] - mn[1]).max(mx[2] - mn[2]);
    if !extent.is_finite() || extent <= 0.0 {
        return data;
    }

    let key_of = |p: &[f32], cell: f32| -> u64 {
        // Reinterpret-sign-bit packing; 21 bits/axis is plenty.
        let ix = ((p[0] - mn[0]) / cell).floor() as i64;
        let iy = ((p[1] - mn[1]) / cell).floor() as i64;
        let iz = ((p[2] - mn[2]) / cell).floor() as i64;
        ((ix as u64) << 42) | ((iy as u64) << 21) | (iz as u64)
    };

    let occupancy = |cell: f32| -> usize {
        let mut set = hashbrown::HashSet::with_capacity(target + 1024);
        for p in pos.chunks_exact(3) {
            set.insert(key_of(p, cell));
        }
        set.len()
    };

    // Binary search: larger cell → fewer occupied cells. Find the size whose
    // occupancy is closest to `target` (favour ≥target so the keep-pass can
    // trim rather than pad).
    let (mut lo, mut hi) = (extent / 8192.0, extent * 0.75);
    let mut cell = hi;
    for _ in 0..14 {
        let mid = (lo * hi).sqrt(); // geometric — occupancy ∝ 1/cell³-ish
        let occ = occupancy(mid);
        cell = mid;
        if (occ as i64 - target as i64).abs() <= 64 {
            break;
        }
        if occ > target {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    // One refinement step toward whichever side sits closer to target.
    let occ = occupancy(cell);
    if occ < target {
        let finer = (lo * cell).sqrt();
        if occupancy(finer) as i64 - target as i64 > 0 {
            cell = finer;
        }
    }

    // Keep the point closest to its voxel centre in every occupied cell.
    let mut best: hashbrown::HashMap<u64, (u32, f32)> =
        hashbrown::HashMap::with_capacity(target + 1024);
    for (i, p) in pos.chunks_exact(3).enumerate() {
        let key = key_of(p, cell);
        let cx = (((key >> 42) as i64) as f32 + 0.5) * cell + mn[0];
        let cy = ((((key >> 21) & 0x1F_FFFF) as i64) as f32 + 0.5) * cell + mn[1];
        let cz = (((key & 0x1F_FFFF) as i64) as f32 + 0.5) * cell + mn[2];
        let d2 = (p[0] - cx).powi(2) + (p[1] - cy).powi(2) + (p[2] - cz).powi(2);
        match best.entry(key) {
            hashbrown::hash_map::Entry::Occupied(mut e) => {
                if d2 < e.get().1 {
                    e.insert((i as u32, d2));
                }
            }
            hashbrown::hash_map::Entry::Vacant(e) => {
                e.insert((i as u32, d2));
            }
        }
    }
    let mut keep: Vec<u32> = best.into_values().map(|(i, _)| i).collect();
    keep.sort_unstable();
    if keep.len() > target {
        // Trim the excess uniformly so coverage stays even.
        let step = keep.len() as f32 / target as f32;
        keep = (0..target)
            .map(|i| keep[(i as f32 * step) as usize])
            .collect();
    }

    let gather = |v: &[f32], stride: usize| -> Vec<f32> {
        keep.iter()
            .flat_map(|&i| {
                v[i as usize * stride..i as usize * stride + stride]
                    .iter()
                    .copied()
            })
            .collect()
    };
    let sh_stride = data.sh_coeffs.as_deref().map_or(0, |c| c.len() / n);
    SplatData {
        means: gather(&data.means, 3),
        rotations: data.rotations.as_deref().map(|v| gather(v, 4)),
        log_scales: data.log_scales.as_deref().map(|v| gather(v, 3)),
        sh_coeffs: data.sh_coeffs.as_deref().map(|v| gather(v, sh_stride)),
        raw_opacities: data.raw_opacities.as_deref().map(|v| gather(v, 1)),
    }
}

pub fn to_init_splats(data: SplatData, mode: SplatRenderMode, device: &Device) -> Splats {
    let n_splats = data.num_splats();

    // Use KNN for scales if not provided
    let log_scales = data
        .log_scales
        .unwrap_or_else(|| compute_knn_scales(&data.means));

    // Default rotation = identity quaternion [1, 0, 0, 0]
    let rotations = data
        .rotations
        .unwrap_or_else(|| [1.0, 0.0, 0.0, 0.0].repeat(n_splats));

    // Default opacity = inverse_sigmoid(0.5)
    let opacities = data
        .raw_opacities
        .unwrap_or_else(|| vec![inverse_sigmoid(0.5); n_splats]);

    // Default SH coeffs = gray (0.5)
    let sh_coeffs = data.sh_coeffs.unwrap_or_else(|| vec![0.5; n_splats * 3]);

    Splats::from_raw(
        data.means, rotations, log_scales, sh_coeffs, opacities, mode, device,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bounds_from_pos_all_nan_does_not_panic() {
        let means = vec![f32::NAN; 30];
        let bb = bounds_from_pos(0.8, &means);
        // We expect a finite fallback — no NaN leak, no panic.
        assert!(bb.center.is_finite(), "center: {:?}", bb.center);
        assert!(bb.extent.is_finite(), "extent: {:?}", bb.extent);
    }

    #[test]
    fn bounds_from_pos_empty_does_not_panic() {
        let bb = bounds_from_pos(0.8, &[]);
        assert!(bb.center.is_finite());
        assert!(bb.extent.is_finite());
    }

    #[test]
    fn bounds_from_pos_mixed_nan_and_finite() {
        // Half NaN, half finite. The finite half should determine the bounds.
        let mut means = Vec::new();
        for i in 0..100 {
            if i % 2 == 0 {
                means.extend_from_slice(&[f32::NAN, f32::NAN, f32::NAN]);
            } else {
                means.extend_from_slice(&[i as f32, i as f32, i as f32]);
            }
        }
        let bb = bounds_from_pos(0.8, &means);
        assert!(bb.center.is_finite());
        assert!(bb.extent.is_finite());
        // Extent should be reasonable (the finite values span 1..99).
        assert!(bb.extent.x > 0.0 && bb.extent.x < 100.0);
    }

    #[test]
    fn bounds_from_pos_one_axis_all_nan() {
        // x and z are OK, y is all NaN — we must not panic indexing into y.
        let mut means = Vec::new();
        for i in 0..50 {
            means.extend_from_slice(&[i as f32, f32::NAN, i as f32]);
        }
        let bb = bounds_from_pos(0.8, &means);
        // y axis collapses to the fallback, other axes should still be
        // reasonable.
        assert!(bb.center.is_finite());
        assert!(bb.extent.is_finite());
    }
}
