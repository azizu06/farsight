use burn::tensor::{ElementConversion, IndexingUpdateOp::Assign, Int, Tensor};

/// Adam with per-parameter second-moment reduction (via [`AdamState::reduce_moment_2`])
/// and per-component learning-rate scaling (via [`AdamState::scaling`]).
///
/// Hand-rolled rather than built on burn's `Adam`, which has neither the
/// per-component LR scaling nor the second-moment reduction below. The
/// trainer also edits momentum tensors in place during refine (split/prune)
/// and swaps the transforms LR scaling every step; burn's low-level
/// `Optimizer` trait does now hand back live `Tensor` state rather than only
/// serialized records, so that part is no longer a blocker — the two missing
/// behaviours are what keep this separate.
#[derive(Clone)]
pub(crate) struct AdamScaled {
    beta_1: f32,
    beta_2: f32,
    epsilon: f32,
}

/// Per-parameter momentum state. When `reduce_moment_2` is set on the owning
/// [`AdamState`], `moment_2` has size 1 in trailing dims; [`AdamState::map_momentum`]
/// callers must stay shape-agnostic along those.
#[derive(Clone)]
pub(crate) struct MomentumState<const D: usize> {
    pub moment_1: Tensor<D>,
    pub moment_2: Tensor<D>,
    pub time: usize,
}

/// Per-parameter optimizer state.
#[derive(Clone)]
pub(crate) struct AdamState<const D: usize> {
    pub momentum: Option<MomentumState<D>>,
    /// Per-component learning rate scaling (e.g. different LR for means vs
    /// rotations vs scales within the transforms tensor).
    pub scaling: Option<Tensor<D>>,
    /// When true, the second moment is reduced to a scalar per row. Set by the
    /// caller when initializing state for parameters where per-element variance
    /// is not needed.
    pub reduce_moment_2: bool,
}

impl<const D: usize> AdamState<D> {
    pub fn new(scaling: Option<Tensor<D>>, reduce_moment_2: bool) -> Self {
        Self {
            momentum: None,
            scaling,
            reduce_moment_2,
        }
    }

    /// Apply `map_fn` to both momentum tensors (no-op before the first step).
    /// `map_fn` must be shape-agnostic along trailing dims since `moment_2`
    /// may have size-1 trailing dims under `reduce_moment_2`.
    pub fn map_momentum(&mut self, map_fn: impl Fn(Tensor<D>) -> Tensor<D>) {
        self.momentum = self.momentum.take().map(|mut moment| {
            moment.moment_1 = map_fn(moment.moment_1);
            moment.moment_2 = map_fn(moment.moment_2);
            moment
        });
    }
}

impl AdamScaled {
    pub fn new(epsilon: f32) -> Self {
        Self {
            beta_1: 0.9,
            beta_2: 0.999,
            epsilon,
        }
    }

    /// One Adam step for a single parameter. `tensor` and `grad` live on the
    /// inner (non-autodiff) backend; `state` is updated in place.
    ///
    /// `grad_sq_mean` supplies the reduced second moment when the caller can
    /// produce it more cheaply than squaring `grad`. It must equal what
    /// `mean_trailing_dims(grad * grad)` would give, and is only read when
    /// [`AdamState::reduce_moment_2`] is set.
    pub fn step<const D: usize>(
        &self,
        lr: f64,
        tensor: Tensor<D>,
        grad: &Tensor<D>,
        grad_sq_mean: Option<Tensor<D>>,
        state: &mut AdamState<D>,
    ) -> Tensor<D> {
        let (grad, momentum) = self.transform(
            grad,
            grad_sq_mean,
            state.momentum.take(),
            state.reduce_moment_2,
        );
        state.momentum = Some(momentum);

        let delta = if let Some(scale) = &state.scaling {
            grad * (scale.clone() * lr).unsqueeze()
        } else {
            grad * lr
        };
        tensor - delta
    }

    fn transform<const D: usize>(
        &self,
        grad: &Tensor<D>,
        grad_sq_mean: Option<Tensor<D>>,
        momentum_state: Option<MomentumState<D>>,
        reduce_moment_2: bool,
    ) -> (Tensor<D>, MomentumState<D>) {
        let grad_sq_for_moment = if reduce_moment_2 && D > 1 {
            grad_sq_mean.unwrap_or_else(|| mean_trailing_dims(grad.clone().powi_scalar(2)))
        } else {
            grad.clone().powi_scalar(2)
        };

        let state = if let Some(mut state) = momentum_state {
            let factor = 1.0 - self.beta_1;
            state.moment_1 = state
                .moment_1
                .mul_scalar(self.beta_1)
                .add(grad.clone().mul_scalar(factor));

            let factor = 1.0 - self.beta_2;
            state.moment_2 = state
                .moment_2
                .mul_scalar(self.beta_2)
                .add(grad_sq_for_moment.mul_scalar(factor));

            state.time += 1;
            state
        } else {
            let factor = 1.0 - self.beta_1;
            let moment_1 = grad.clone().mul_scalar(factor);

            let factor = 1.0 - self.beta_2;
            let moment_2 = grad_sq_for_moment.mul_scalar(factor);

            MomentumState {
                moment_1,
                moment_2,
                time: 1,
            }
        };

        let time = (state.time as i32).elem();
        let moment_1_corrected = state
            .moment_1
            .clone()
            .mul_scalar(1f32 / (1f32 - self.beta_1.powi(time)));
        let moment_2_corrected = state
            .moment_2
            .clone()
            .mul_scalar(1f32 / (1f32 - self.beta_2.powi(time)));
        // moment_2_corrected broadcasts when it has reduced trailing dims
        let grad = moment_1_corrected.div(moment_2_corrected.sqrt().add_scalar(self.epsilon));
        (grad, state)
    }

    /// Sparse variant: update only the rows listed in `inds` (the splats
    /// visible in the current view, Taming-3DGS SparseAdam style). Rows not
    /// listed get neither a parameter update nor momentum decay — their
    /// momentum is left as-is rather than decayed towards zero, so a splat
    /// re-entering view resumes where it left off. Compute runs on the
    /// compact `[V, ...]` gather, not the dense `[N, ...]` tensors.
    ///
    /// `grad` is the dense per-splat gradient; invisible rows are expected
    /// to be zero (the backward expands compact grads through a zero row).
    /// `grad_sq_mean` is the reduced second moment on the same dense
    /// indexing; it is gathered along with everything else.
    pub fn step_sparse<const D: usize>(
        &self,
        lr: f64,
        tensor: &Tensor<D>,
        grad: &Tensor<D>,
        inds: &Tensor<1, Int>,
        grad_sq_mean: Option<Tensor<D>>,
        state: &mut AdamState<D>,
    ) -> Tensor<D> {
        if inds.dims()[0] == 0 {
            return tensor.clone();
        }

        let grad_c = grad.clone().select(0, inds.clone());
        let param_c = tensor.clone().select(0, inds.clone());
        let grad_sq_c = grad_sq_mean.map(|m| m.select(0, inds.clone()));

        // Pull the momentum rows for the visible set (zeros for a
        // first-visit row — matching the dense init path in `transform`).
        let reduce = state.reduce_moment_2;
        let time = state.momentum.as_ref().map_or(0, |m| m.time);
        let (m1_c, m2_c) = match &state.momentum {
            Some(m) => (
                m.moment_1.clone().select(0, inds.clone()),
                m.moment_2.clone().select(0, inds.clone()),
            ),
            None => (
                Tensor::zeros(param_c.dims(), &param_c.device()),
                Tensor::zeros(m2_dims(param_c.dims(), reduce), &param_c.device()),
            ),
        };

        let (grad_c, compact) = self.transform(
            &grad_c,
            grad_sq_c,
            Some(MomentumState {
                moment_1: m1_c,
                moment_2: m2_c,
                time,
            }),
            reduce,
        );

        // Scatter the updated momentum rows back into the dense state.
        let (m1_dense, m2_dense) = match state.momentum.take() {
            Some(m) => (m.moment_1, m.moment_2),
            None => (
                Tensor::zeros(tensor.dims(), &tensor.device()),
                Tensor::zeros(m2_dims(tensor.dims(), reduce), &tensor.device()),
            ),
        };
        state.momentum = Some(MomentumState {
            moment_1: m1_dense.select_assign(0, inds.clone(), compact.moment_1, Assign),
            moment_2: m2_dense.select_assign(0, inds.clone(), compact.moment_2, Assign),
            time: compact.time,
        });

        let delta = if let Some(scale) = &state.scaling {
            grad_c * (scale.clone() * lr).unsqueeze()
        } else {
            grad_c * lr
        };
        tensor
            .clone()
            .select_assign(0, inds.clone(), param_c - delta, Assign)
    }
}

/// Shape of `moment_2` for a parameter: full shape normally, size-1
/// trailing dims under `reduce_moment_2`.
fn m2_dims<const D: usize>(mut dims: [usize; D], reduce: bool) -> [usize; D] {
    if reduce {
        for d in &mut dims[1..] {
            *d = 1;
        }
    }
    dims
}

/// Reduce to a single mean per row by averaging across all trailing dims (1..D).
/// Result has size 1 in each trailing dim so it broadcasts back to the full shape.
fn mean_trailing_dims<const D: usize>(t: Tensor<D>) -> Tensor<D> {
    debug_assert!(D > 1, "mean_trailing_dims requires D > 1");
    let shape = t.dims();
    let trailing_count: usize = shape[1..].iter().product();

    // Reduce over the trailing dims directly; a flatten + reshape would end
    // burn's fusion block.
    let dims: Vec<usize> = (1..D).collect();
    t.sum_dims(&dims) / trailing_count as f32
}
