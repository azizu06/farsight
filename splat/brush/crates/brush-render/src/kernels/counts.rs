use burn::cubecl;
use burn::cubecl::cube;
use burn::cubecl::prelude::*;

/// Collapses the project_forward atomics into a packed counts buffer:
/// `counts[0]` = num_visible, `counts[1]` = min(num_intersections, isect_cap).
/// Runs as a single-cube launch right after project_forward so every
/// downstream dispatch can be sized by capacity and bound in-kernel —
/// no GPU->CPU readback is needed inside `render()`.
#[cube(launch)]
pub fn write_counts(
    num_visible: &Tensor<u32>,
    num_intersections: &Tensor<u32>,
    counts: &mut Tensor<u32>,
    isect_cap: u32,
) {
    if ABSOLUTE_POS == 0 {
        counts[0] = num_visible[0];
        counts[1] = min(num_intersections[0], isect_cap);
    }
}
