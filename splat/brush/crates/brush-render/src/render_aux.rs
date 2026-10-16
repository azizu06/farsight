use burn::{
    Tensor,
    backend::{
        Backend, ExtensionType,
        tensor::{FloatTensor, IntTensor},
    },
    tensor::Int,
};

use crate::shaders::helpers::ProjectUniforms;

/// Internal render output used by kernel impls. Holds backend primitives.
///
/// `ExtensionType` lets the `#[backend_extension]`-generated `Dispatch` impl
/// re-wrap these primitives across the backend boundary automatically.
#[derive(Debug, Clone, ExtensionType)]
pub struct RenderOutput<B: Backend> {
    pub out_img: FloatTensor<B>,
    #[extension_type]
    pub aux: RenderAuxInner<B>,
    // State needed by the backward pass; non-diff callers can ignore these.
    pub projected_splats: FloatTensor<B>,
    pub compact_gid_from_isect: IntTensor<B>,
    pub project_uniforms: ProjectUniforms,
    pub global_from_compact_gid: IntTensor<B>,
    /// Inverse of `global_from_compact_gid` over all splats, offset by one:
    /// visible splat `compact_gid` holds `compact_gid + 1`, culled splats 0.
    /// Row 0 of the backward's compact gradient buffers is a zero row, so a
    /// plain gather through this expands them to the dense param shape.
    /// Forward-only passes keep an unused one-element placeholder instead.
    pub compact_from_global: IntTensor<B>,
}

impl<B: Backend> RenderOutput<B> {
    /// Full validation; gated on `debug-validation` feature / `cfg(test)`.
    /// Takes self by value to avoid Send issues with the async readbacks.
    ///
    /// Reads `aux.counts` — a GPU sync — but validation only runs on debug
    /// paths, never in the training hot loop.
    #[allow(unused_variables)]
    pub async fn validate(self) {
        #[cfg(any(feature = "debug-validation", test))]
        {
            let data = <B as burn::backend::ops::IntTensorOps<B>>::int_into_data(
                self.aux.counts,
            )
            .await
            .expect("counts readback");
            let counts: &[u32] = data.as_slice::<u32>().expect("counts dtype");
            let (num_visible, num_intersections) = (counts[0], counts[1]);
            let total_splats = self.project_uniforms.total_splats;
            assert!(
                num_visible <= total_splats,
                "num_visible ({num_visible}) > total_splats ({total_splats})",
            );
            let max_isects = (num_visible as u64)
                * (self.project_uniforms.tile_bounds[0] as u64)
                * (self.project_uniforms.tile_bounds[1] as u64);
            assert!(
                (num_intersections as u64) <= max_isects,
                "num_intersections ({num_intersections}) > max possible {max_isects}",
            );
        }
        // Heavy validation lives at the public API boundary where we have Tensor<D>.
    }
}

/// Internal aux struct holding backend primitives. Used by the kernel
/// pipeline and the backward registration.
#[derive(Debug, Clone, ExtensionType)]
pub struct RenderAuxInner<B: Backend> {
    /// GPU-side `[num_visible, isect_used, isect_total]` written by the
    /// write_counts / seal_isects kernels. `isect_used` is clamped to the
    /// isect buffer capacity; `isect_total` is the true unclamped count —
    /// `isect_total > isect_used` means intersections were dropped this
    /// step. Read it back on your own schedule — reading forces a sync, so
    /// the training loop defers it until the rest of the step is queued.
    pub counts: IntTensor<B>,
    pub visible: FloatTensor<B>,
    /// Per-splat maximum screen-space radius in pixels (global-gid indexed).
    /// Zero for splats that were culled / invisible in this view.
    pub max_radius: FloatTensor<B>,
    /// Per-splat opacity with the scale floor folded in (global-gid indexed),
    /// zero for culled splats.
    pub opacities: FloatTensor<B>,
    pub tile_offsets: IntTensor<B>,
    pub img_size: glam::UVec2,
}

/// Public, backend-agnostic aux. Holds `Tensor<D>` for the user.
#[derive(Debug, Clone)]
pub struct RenderAux {
    /// GPU-side `[num_visible, isect_used]`. Read lazily — forces a sync.
    pub counts: Tensor<1, Int>,
    pub visible: Tensor<1>,
    /// Per-splat maximum screen-space radius in pixels (global-gid indexed).
    /// Zero for splats that were culled / invisible in this view.
    pub max_radius: Tensor<1>,
    /// Per-splat opacity with the scale floor folded in (global-gid indexed),
    /// zero for culled splats.
    pub opacities: Tensor<1>,
    pub tile_offsets: Tensor<3, Int>,
    pub img_size: glam::UVec2,
}
