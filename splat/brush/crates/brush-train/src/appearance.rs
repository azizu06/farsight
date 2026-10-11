//! Per-view appearance affine bank (T6): applied to the rendered RGB
//! before the image loss — `rgb' = rgb @ gain + bias` (12 params/view).
//! Learns to undo per-frame auto-exposure / auto-white variation on phone
//! captures (nerf-w style per-view appearance). Params are burn autodiff
//! leaf tensors: their grads come out of the shared `loss.backward()`, and
//! this module steps them with its own low-LR [`AdamScaled`].
//!
//! Warm-up is the caller's job (gate [`Self::apply`] on schedule
//! progress) — params are created lazily at identity on first apply, so
//! nothing exists to disturb early geometry. [`Self::penalty`] adds a
//! small L2 pull toward identity to the view loss.

use std::collections::HashMap;

use brush_loss::psnr_from_mse;
use burn::tensor::{Device, Gradients, Tensor, s};

use crate::adam_scaled::{AdamScaled, AdamState};

const EYE: [[f32; 3]; 3] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];

struct ViewAffine {
    /// `[3,3]` colour map.
    gain: Tensor<2>,
    /// `[3]` additive term.
    bias: Tensor<1>,
    gain_opt: AdamState<2>,
    bias_opt: AdamState<1>,
}

/// Bank of per-view affines, stepped at `lr` once per training iteration.
pub(crate) struct AppearanceBank {
    views: HashMap<u32, ViewAffine>,
    adam: AdamScaled,
    /// Step size; `<= 0` disables the bank.
    lr: f64,
    /// Identity-regulariser weight folded into the view loss.
    reg: f64,
}

impl AppearanceBank {
    pub fn new(lr: f64, reg: f64) -> Self {
        Self {
            views: HashMap::new(),
            adam: AdamScaled::new(1e-8),
            lr,
            reg,
        }
    }

    pub fn enabled(&self) -> bool {
        self.lr > 0.0
    }

    /// Lazily create `view`'s affine at identity on the *autodiff* device —
    /// leaf nodes so the shared backward writes their grads into `Gradients`.
    fn entry(&mut self, view: u32, device: &Device) -> &mut ViewAffine {
        self.views.entry(view).or_insert_with(|| ViewAffine {
            gain: Tensor::<2>::from_floats(EYE, device).require_grad(),
            bias: Tensor::<1>::from_floats([0.0; 3], device).require_grad(),
            gain_opt: AdamState::new(None, false),
            bias_opt: AdamState::new(None, false),
        })
    }

    /// `pred [H, W, C]` (RGB in the first 3 channels) → affine RGB, other
    /// channels passed through untouched (alpha compositing in the loss
    /// kernels sees the original alpha).
    pub fn apply(&mut self, view: u32, pred: Tensor<3>, device: &Device) -> Tensor<3> {
        let [h, w, c] = pred.dims();
        if c < 3 {
            return pred;
        }
        let e = self.entry(view, device);
        let rgb = pred.clone().slice(s![.., .., 0..3]);
        let rgb = rgb
            .reshape([h * w, 3])
            .matmul(e.gain.clone())
            .add(e.bias.clone().unsqueeze())
            .reshape([h, w, 3]);
        if c == 3 {
            rgb
        } else {
            let rest = pred.slice(s![.., .., 3..c]);
            Tensor::cat(vec![rgb, rest], 2)
        }
    }

    /// Identity regulariser for `view` (call right after `apply`):
    /// `reg * (|gain - I|^2 + |bias|^2)` as a differentiable scalar loss.
    pub fn penalty(&mut self, view: u32, device: &Device) -> Tensor<1> {
        let e = self.entry(view, device);
        let eye = Tensor::<2>::from_floats(EYE, device);
        let dg = (e.gain.clone() - eye).powi_scalar(2).sum();
        let db = e.bias.clone().powi_scalar(2).sum();
        (dg + db).mul_scalar(self.reg)
    }

    /// Step every affine touched this iteration. Grads for views absent
    /// from this step's graph simply aren't in `grads`.
    pub fn step(&mut self, grads: &mut Gradients) {
        if !self.enabled() {
            return;
        }
        for e in self.views.values_mut() {
            e.gain = step_tensor(&self.adam, self.lr, e.gain.clone(), &mut e.gain_opt, grads);
            e.bias = step_tensor(&self.adam, self.lr, e.bias.clone(), &mut e.bias_opt, grads);
        }
    }
}

fn step_tensor<const D: usize>(
    adam: &AdamScaled,
    lr: f64,
    param: Tensor<D>,
    state: &mut AdamState<D>,
    grads: &mut Gradients,
) -> Tensor<D> {
    let Some(grad) = param.grad_remove(grads) else {
        return param;
    };
    let inner = param.inner();
    let stepped = adam.step(lr, inner, &grad, None, state);
    Tensor::from_inner(stepped).require_grad()
}

/// Appearance-fair PSNR for an eval view: per-channel affine fitted by
/// least squares `gt ≈ g·pred + b` on the LEFT half of the frame, applied
/// to the right half, PSNR scored on the right half only. This is the
/// honest counterpart of the train-time bank: the affine generalises
/// across space instead of being memorised per pixel.
///
/// `pred`/`gt_rgb` are `[H, W, 3]` f32 in `[0, 1]` on the plain device.
pub fn eval_appearance_psnr(pred: Tensor<3>, gt_rgb: Tensor<3>) -> Option<Tensor<1>> {
    let [h, w, c] = pred.dims();
    let wl = w / 2;
    if c != 3 || wl == 0 || w == wl {
        return None;
    }
    let pl = pred.clone().slice(s![.., 0..wl, ..]).reshape([h * wl, c]);
    let gl = gt_rgb.clone().slice(s![.., 0..wl, ..]).reshape([h * wl, c]);
    // Per-channel least squares over the left half.
    let mp = pl.clone().mean_dim(0); // [1, 3]
    let mg = gl.clone().mean_dim(0);
    let cov = (pl.clone() * gl.clone()).mean_dim(0) - mp.clone() * mg.clone();
    let var = (pl.clone() * pl.clone()).mean_dim(0) - mp.clone() * mp.clone();
    let g = (cov / var.clamp_min(1e-6)).clamp(0.25, 4.0);
    let b = mg - g.clone() * mp;
    // Apply to the right half and score there.
    let wr = w - wl;
    let pr = pred.slice(s![.., wl..w, ..]).reshape([h * wr, c]);
    let gr = gt_rgb.slice(s![.., wl..w, ..]).reshape([h * wr, c]);
    let fit = pr * g + b;
    let mse = (fit - gr).powi_scalar(2).mean();
    Some(psnr_from_mse(mse))
}
