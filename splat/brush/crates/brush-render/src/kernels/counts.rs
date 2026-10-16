use burn::cubecl;
use burn::cubecl::cube;
use burn::cubecl::prelude::*;

/// Writes `counts[0]` = num_visible right after project_forward so every
/// downstream dispatch can be sized by capacity and bound in-kernel —
/// no GPU->CPU readback is needed inside `render()`.
#[cube(launch)]
pub fn write_counts(num_visible: &Tensor<u32>, counts: &mut Tensor<u32>) {
    if ABSOLUTE_POS == 0 {
        counts[0] = num_visible[0];
    }
}

/// Seals `counts[1]` = min(total intersections, isect_cap) once the prefix
/// scan exists: total isects is exactly `cum_tiles_hit[num_visible - 1]`
/// (inclusive scan). Runs as a single-cube launch after `prefix_sum`, before
/// the first consumer (tile sort length, `get_tile_offsets`).
/// Replaces the per-splat `num_intersections` global atomic in
/// project_forward.
#[cube(launch)]
pub fn seal_isects(cum_tiles_hit: &Tensor<u32>, counts: &mut Tensor<u32>, isect_cap: u32) {
    if ABSOLUTE_POS == 0 {
        let nv = counts[0];
        let total = if nv > 0u32 { cum_tiles_hit[(nv - 1u32) as usize] } else { 0u32 };
        counts[1] = min(total, isect_cap);
        // counts[2] keeps the unclamped total so the train loop's deferred
        // readback can size next step's isect capacity (and spot drops when
        // counts[2] > counts[1]).
        counts[2] = total;
    }
}
